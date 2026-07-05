from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import secrets
import signal
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import qrcode
import yaml
from PIL import Image
from telethon import TelegramClient, events, functions, types
from telethon.errors import RPCError


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ACCOUNTS = ("owner", "user_a", "user_b")


class E2EFailure(AssertionError):
    pass


def _print_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def authorize(client: TelegramClient) -> None:
    if await client.is_user_authorized():
        return

    try:
        qr = await client.qr_login()
        _print_qr(qr.url)
        await qr.wait(timeout=120)

    except:
        password = input("Enter 2FA password: ")
        await client.sign_in(password=password)


@dataclass(slots=True)
class Account:
    name: str
    client: TelegramClient
    user_id: int | None = None
    bot_entity: Any = None


class Harness:
    def __init__(self, config_path: Path, *, allow_login_prompt: bool = False):
        self.config_path = config_path
        self.config = _load_yaml(config_path)
        self.allow_login_prompt = allow_login_prompt
        self.bot_username = _bot_username(
            str(_require(self.config, "bot.username")))
        self.bot_process: subprocess.Popen | None = None
        self.bot_log_handle = None
        self.accounts: dict[str, Account] = {}
        self.history: dict[str, list[Any]] = defaultdict(list)
        self.queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.deleted_ids: dict[str, set[int]] = defaultdict(set)
        self.deleted_queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.handlers: list[tuple[TelegramClient, Any, Any]] = []
        self.message_timeout = float(
            _get(self.config, "timeouts.message_seconds", 30))
        self.started_all = False

    async def __aenter__(self) -> "Harness":
        await self.connect_accounts()
        await self.resolve_bot_entities()
        if bool(_get(self.config, "bot.launch.enabled", True)):
            self.launch_bot()
            await asyncio.sleep(float(_get(self.config, "timeouts.bot_start_seconds", 8)))
            self.assert_bot_running()
        self.install_inboxes()
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            if self.bot_process:
                self.assert_no_bot_conflict()
        finally:
            for client, handler, builder in self.handlers:
                client.remove_event_handler(handler, builder)
            for account in self.accounts.values():
                await account.client.disconnect()
            self.stop_bot()

    def assert_no_bot_conflict(self) -> None:
        log_file = _resolve_path(str(_get(self.config, "bot.launch.log_file", "e2e/.runtime/bot.log")))
        if not log_file.exists():
            return
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
        if "telegram.error.Conflict" not in log_text:
            return
        tail = "\n".join(log_text.splitlines()[-80:])
        raise E2EFailure(f"Launched bot hit Telegram polling conflict; e2e result is invalid.\n{tail}")

    async def connect_accounts(self) -> None:
        account_cfg = self.config.get("accounts") or {}
        for name in REQUIRED_ACCOUNTS:
            if name not in account_cfg:
                raise E2EFailure(
                    f"Missing accounts.{name} in {self.config_path}")
            session = _resolve_path(
                str(_require(account_cfg[name], "session")))
            api_id = int(account_cfg[name].get(
                "api_id") or _get(self.config, "telegram.api_id") or 0)
            api_hash = str(account_cfg[name].get(
                "api_hash") or _get(self.config, "telegram.api_hash") or "")
            if not api_id or not api_hash:
                raise E2EFailure(f"Missing accounts.{name}.api_id/api_hash")
            session.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(str(session), api_id, api_hash)
            print("Logging in account", name, "...")
            await client.connect()
            await authorize(client)

            me = await client.get_me()
            self.accounts[name] = Account(
                name=name,
                client=client,
                user_id=int(me.id),
            )
            print(
                f"{name}: logged in as {me.id} @{getattr(me, 'username', None) or '-'}")

    async def resolve_bot_entities(self) -> None:
        for account in self.accounts.values():
            account.bot_entity = await account.client.get_entity(self.bot_username)

    def install_inboxes(self) -> None:
        for account in self.accounts.values():
            new_builder = events.NewMessage(
                chats=account.bot_entity, incoming=True)
            edit_builder = events.MessageEdited(chats=account.bot_entity)
            delete_builder = events.MessageDeleted(chats=account.bot_entity)

            async def handler(event, account_name=account.name):
                self.history[account_name].append(event.message)
                await self.queues[account_name].put(event.message)

            async def delete_handler(event, account_name=account.name):
                for message_id in event.deleted_ids:
                    self.deleted_ids[account_name].add(int(message_id))
                    await self.deleted_queues[account_name].put(int(message_id))

            account.client.add_event_handler(handler, new_builder)
            self.handlers.append((account.client, handler, new_builder))
            account.client.add_event_handler(handler, edit_builder)
            self.handlers.append((account.client, handler, edit_builder))
            account.client.add_event_handler(delete_handler, delete_builder)
            self.handlers.append((account.client, delete_handler, delete_builder))

    def launch_bot(self) -> None:
        owner_id = self.accounts["owner"].user_id
        if owner_id is None:
            raise E2EFailure("Owner account has no resolved user id.")
        launch = _get(self.config, "bot.launch", {})
        runtime_config = _resolve_path(
            str(launch.get("runtime_config", "e2e/.runtime/bot.config.yml")))
        runtime_db = _resolve_path(
            str(launch.get("runtime_database", "e2e/.runtime/bot.db")))
        log_file = _resolve_path(
            str(launch.get("log_file", "e2e/.runtime/bot.log")))
        runtime_config.parent.mkdir(parents=True, exist_ok=True)
        runtime_db.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if bool(launch.get("fresh_database", True)):
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(runtime_db) + suffix)
                if candidate.exists():
                    candidate.unlink()
        bot_config = self._runtime_bot_config(runtime_db, owner_id)
        runtime_config.write_text(yaml.safe_dump(
            bot_config, sort_keys=False), encoding="utf-8")

        python = _resolve_path(
            str(launch.get("python", "../fb/venv/Scripts/python.exe")))
        if not python.exists():
            python = Path(sys.executable)
        run_py = _resolve_path(str(launch.get("run_py", "run.py")))
        self.bot_log_handle = log_file.open("w", encoding="utf-8")
        env = os.environ.copy()
        env["BOT_TOKEN"] = str(_require(self.config, "bot.token"))
        env["PYTHONUNBUFFERED"] = "1"
        self.bot_process = subprocess.Popen(
            [str(python), str(run_py), "--config", str(runtime_config)],
            cwd=str(ROOT),
            env=env,
            stdout=self.bot_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        print(f"bot: launched pid={self.bot_process.pid}, log={log_file}")

    def assert_bot_running(self) -> None:
        if not self.bot_process or self.bot_process.poll() is None:
            return
        log_text = ""
        if self.bot_log_handle:
            self.bot_log_handle.flush()
        log_file = _resolve_path(str(_get(self.config, "bot.launch.log_file", "e2e/.runtime/bot.log")))
        if log_file.exists():
            log_text = "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
        raise E2EFailure(f"Launched bot exited during startup with rc={self.bot_process.returncode}.\n{log_text}")

    def _runtime_bot_config(self, runtime_db: Path, owner_id: int) -> dict[str, Any]:
        launch = _get(self.config, "bot.launch", {})
        base_path = _resolve_path(str(launch.get("base_config", "config.yml")))
        bot_config = _load_yaml(base_path)
        _set(bot_config, "bot.token", str(_require(self.config, "bot.token")))
        _set(bot_config, "bot.admin_ids", [owner_id])
        _set(bot_config, "database.path", str(runtime_db))
        _set(bot_config, "database.migrate_from", None)
        _set(bot_config, "logging.level", "INFO")
        _set(bot_config, "logging.aggregate_interval_seconds", 10)
        _set(bot_config, "cache.transient_message_ttl_hours", 6)
        _set(bot_config, "delivery.telegram_rate_limit_per_second", 30)
        _set(bot_config, "delivery.worker_count", 4)
        _set(bot_config, "rate_limits.message_send_limit", 100)
        _set(bot_config, "rate_limits.window_seconds", 10)
        _set(bot_config, "credits.starting_balance", 1000.0)
        _set(bot_config, "credits.daily_tax_enabled", False)
        _set(bot_config, "credits.negative_credit_cooldown_seconds", 0)
        _set(bot_config, "onboarding.initial_cooldown_seconds", 0)
        _set(bot_config, "tips.enabled", False)
        _set(bot_config, "inactivity.period_days", 3650)
        _set(bot_config, "inactivity.non_system_receive_chance", 1.0)
        _set(bot_config, "loss_rate.schedule", [
             {"credits": 0, "loss_rate": 0.0}, {"credits": 100000, "loss_rate": 0.0}])
        _set(bot_config, "saucenao.enabled", False)
        _set(bot_config, "ai.enabled", False)
        _set(bot_config, "vote_to_remove.threshold", 2)
        _set(bot_config, "vote_to_remove.user_vote_cooldown_seconds", 0.001)
        _set(bot_config, "vote_to_remove.user_remove_limit", 100)
        _set(bot_config, "vote_to_remove.user_remove_cooldown_seconds", 3600)
        _set(bot_config, "vote_to_remove.global_limit", 100)
        _set(bot_config, "vote_to_remove.global_cooldown_seconds", 3600)
        _set(bot_config, "vote_to_remove.collateral_remove_amount", 0)
        _set(bot_config, "vote_to_remove.punishment_cooldown_seconds", 0.001)
        _set(bot_config, "vote_to_remove.voter_min_top_credit_percentile", None)
        return bot_config

    def stop_bot(self) -> None:
        if self.bot_process:
            if self.bot_process.poll() is None:
                try:
                    self.bot_process.send_signal(signal.SIGTERM)
                    self.bot_process.wait(timeout=10)
                except Exception:
                    self.bot_process.kill()
                    self.bot_process.wait(timeout=10)
            print(f"bot: stopped rc={self.bot_process.returncode}")
        if self.bot_log_handle:
            self.bot_log_handle.close()

    async def start_all(self) -> None:
        if self.started_all:
            return
        for name in REQUIRED_ACCOUNTS:
            await self.send_text(name, "/start")
        for name in REQUIRED_ACCOUNTS:
            await self.wait_for(name, lambda msg: bool(msg.raw_text), label=f"{name} /start reply")
        self.started_all = True

    async def send_text(self, account_name: str, text: str, *, reply_to: Any = None):
        account = self.accounts[account_name]
        return await account.client.send_message(account.bot_entity, text, reply_to=reply_to)

    async def send_file(self, account_name: str, file_path: Path, *, caption: str = ""):
        account = self.accounts[account_name]
        return await account.client.send_file(account.bot_entity, file=str(file_path), caption=caption)

    async def forward_to_bot(self, account_name: str, message: Any):
        account = self.accounts[account_name]
        return await account.client.forward_messages(account.bot_entity, message)

    async def send_reaction(self, account_name: str, message: Any, emoji: str) -> None:
        account = self.accounts[account_name]
        try:
            await account.client.send_reaction(account.bot_entity, message, reaction=emoji)
        except (AttributeError, TypeError):
            await account.client(
                functions.messages.SendReactionRequest(
                    peer=account.bot_entity,
                    msg_id=int(getattr(message, "id", message)),
                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                )
            )
        except RPCError as exc:
            raise E2EFailure(
                f"Telegram rejected reaction {emoji!r}: {exc}") from exc

    async def click_button(self, account_name: str, message: Any, text: str) -> None:
        labels = self.button_texts(message)
        if text not in labels:
            raise E2EFailure(
                f"Button {text!r} not found. Available buttons: {labels}")
        try:
            await message.click(text=text)
        except RPCError as exc:
            raise E2EFailure(
                f"Telegram rejected button click {text!r}: {exc}") from exc

    def button_texts(self, message: Any) -> list[str]:
        labels: list[str] = []
        for row in message.buttons or []:
            for button in row:
                labels.append(str(getattr(button, "text", "")))
        return labels

    def clear_inboxes(self) -> None:
        self.history.clear()
        self.deleted_ids.clear()
        for queue in self.queues.values():
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        for queue in self.deleted_queues.values():
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def wait_text(self, account_name: str, needle: str, *, timeout: float | None = None):
        return await self.wait_for(
            account_name,
            lambda msg: _message_id(msg) is not None and needle in (msg.raw_text or ""),
            label=f"{account_name} message containing {needle!r}",
            timeout=timeout,
        )

    async def wait_reply_text(self, account_name: str, needle: str, reply_to: Any, *, timeout: float | None = None):
        expected = _message_id(reply_to) if not isinstance(reply_to, int) else reply_to
        if expected is None:
            raise E2EFailure(f"Cannot wait for reply to id-less message: {reply_to!r}")
        return await self.wait_for(
            account_name,
            lambda msg: _message_id(msg) is not None and needle in (msg.raw_text or "") and getattr(msg, "reply_to_msg_id", None) == expected,
            label=f"{account_name} message containing {needle!r} replying to {expected}",
            timeout=timeout,
        )

    async def wait_for(
        self,
        account_name: str,
        predicate: Callable[[Any], bool],
        *,
        label: str,
        timeout: float | None = None,
    ):
        timeout = self.message_timeout if timeout is None else timeout
        for msg in self.history[account_name]:
            if predicate(msg):
                return msg
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise E2EFailure(f"Timed out waiting for {label}")
            try:
                msg = await asyncio.wait_for(self.queues[account_name].get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise E2EFailure(f"Timed out waiting for {label}") from exc
            if predicate(msg):
                return msg

    async def wait_deleted(self, account_name: str, message: Any, *, timeout: float | None = None) -> None:
        expected = _message_id(message) if not isinstance(message, int) else message
        if expected is None:
            raise E2EFailure(f"Cannot wait for deletion of id-less message: {message!r}")
        if expected in self.deleted_ids[account_name]:
            return
        timeout = self.message_timeout if timeout is None else timeout
        account = self.accounts[account_name]
        try:
            current = await account.client.get_messages(account.bot_entity, ids=expected)
            if not current:
                return
        except RPCError:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                current = await account.client.get_messages(account.bot_entity, ids=expected)
                if not current:
                    return
                raise E2EFailure(f"Timed out waiting for {account_name} message {expected} to be deleted")
            try:
                message_id = await asyncio.wait_for(self.deleted_queues[account_name].get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise E2EFailure(f"Timed out waiting for {account_name} message {expected} to be deleted") from exc
            if message_id == expected:
                return

    async def fetch_bot_message(self, account_name: str, message: Any):
        expected = _message_id(message) if not isinstance(message, int) else message
        if expected is None:
            raise E2EFailure(f"Cannot fetch id-less message: {message!r}")
        account = self.accounts[account_name]
        return await account.client.get_messages(account.bot_entity, ids=expected)

    async def wait_reaction(self, account_name: str, message: Any, emoji: str, *, timeout: float | None = None) -> None:
        expected = _message_id(message) if not isinstance(message, int) else message
        if expected is None:
            raise E2EFailure(f"Cannot wait for reaction on id-less message: {message!r}")
        timeout = self.message_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_reactions = None
        while True:
            current = await self.fetch_bot_message(account_name, expected)
            last_reactions = getattr(current, "reactions", None) if current else None
            if current and _message_has_reaction(current, emoji):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise E2EFailure(f"Timed out waiting for {emoji!r} reaction on {account_name} message {expected}; reactions={last_reactions!r}")
            await asyncio.sleep(min(0.5, remaining))

    async def assert_absent_text(self, account_name: str, needle: str, *, timeout: float | None = None) -> None:
        timeout = float(_get(self.config, "timeouts.absent_seconds", 3)
                        ) if timeout is None else timeout
        try:
            msg = await self.wait_text(account_name, needle, timeout=timeout)
        except E2EFailure:
            return
        raise E2EFailure(
            f"Unexpected {account_name} message containing {needle!r}: {msg.raw_text!r}")

    def marker(self, prefix: str) -> str:
        return f"E2E-{prefix}-{secrets.token_hex(5)}"

    def runtime_path(self, name: str) -> Path:
        path = _resolve_path(f"e2e/.runtime/{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def assert_reply_to(self, message: Any, target: Any, label: str) -> None:
        expected = _message_id(target) if not isinstance(target, int) else target
        if expected is None:
            raise E2EFailure(f"{label} target has no usable message id: {target!r}")
        actual = getattr(message, "reply_to_msg_id", None)
        if actual != expected:
            raise E2EFailure(f"{label} did not reply to expected message: got {actual}, expected {expected}")


async def scenario_basic(h: Harness) -> None:
    await h.start_all()
    marker = h.marker("basic")
    source = await h.send_text("user_a", marker)
    forwarded_to_b = await h.wait_text("user_b", marker)
    await h.wait_text("owner", marker)

    reply_marker = h.marker("reply")
    await h.send_text("user_b", reply_marker, reply_to=forwarded_to_b)
    reply_to_sender = await h.wait_text("user_a", reply_marker)
    h.assert_reply_to(reply_to_sender, source, "Forwarded reply")
    print("PASS basic")


async def scenario_duplicate_media(h: Harness) -> None:
    await h.start_all()
    image_path = h.runtime_path("duplicate.png")
    _make_test_image(image_path, variant="duplicate")
    first = h.marker("media-first")
    second = h.marker("media-second")
    await h.send_file("user_a", image_path, caption=first)
    await h.wait_text("user_b", first)
    second_source = await h.send_file("user_a", image_path, caption=second)
    rejection = await h.wait_text("user_a", "Duplicate media was rejected.")
    h.assert_reply_to(rejection, second_source, "Duplicate rejection")
    await h.assert_absent_text("user_b", second)
    print("PASS duplicate-media")


async def scenario_moderation(h: Harness) -> None:
    await h.start_all()
    user_b_id = h.accounts["user_b"].user_id
    await h.send_text("owner", f"/mod {user_b_id}")
    await h.wait_text("owner", "promoted to moderator")

    modsay_marker = h.marker("modsay")
    await h.send_text("user_b", f"/modsay {modsay_marker}")
    await h.wait_text("user_a", modsay_marker)

    await h.send_text("owner", f"/unmod {user_b_id}")
    await h.wait_text("owner", "removed as moderator")

    delete_marker = h.marker("delete")
    await h.send_text("user_a", delete_marker)
    forwarded_owner = await h.wait_text("owner", delete_marker)
    forwarded_b = await h.wait_text("user_b", delete_marker)
    await h.send_text("owner", "/delete", reply_to=forwarded_owner)
    deleted_reply = await h.wait_text("owner", "Deleted (")
    h.assert_reply_to(deleted_reply, forwarded_owner, "Admin delete confirmation")
    await h.wait_for(
        "user_b",
        lambda msg: msg.id == forwarded_b.id and "Message removed." in (
            msg.raw_text or ""),
        label="user_b tombstone edit after admin delete",
    )
    print("PASS moderation")


async def scenario_reactions(h: Harness) -> None:
    await h.start_all()
    marker = h.marker("react")
    source = await h.send_text("user_a", marker)
    forwarded_to_b = await h.wait_text("user_b", marker)
    await h.send_reaction("user_b", forwarded_to_b, "👍")
    voter_notice = await h.wait_text("user_b", "Upvote sent.")
    sender_notice = await h.wait_text("user_a", "Upvote received:")
    h.assert_reply_to(voter_notice, forwarded_to_b, "Voter upvote notice")
    h.assert_reply_to(sender_notice, source, "Sender upvote notice")
    print("PASS reactions")


async def _delete_vote_flow(h: Harness, prefix: str):
    await h.start_all()
    h.clear_inboxes()
    marker = h.marker(prefix)
    source = await h.send_text("user_a", marker)
    owner_copy = await h.wait_text("owner", marker)
    user_b_copy = await h.wait_text("user_b", marker)

    await h.send_text("user_b", "/deletevote", reply_to=user_b_copy)
    first_vote = await h.wait_reply_text("user_b", "Remove vote recorded (1/2)", user_b_copy)
    await h.send_text("owner", "/deletevote", reply_to=owner_copy)
    threshold_vote = await h.wait_reply_text("owner", "Remove vote threshold reached. Message removed.", owner_copy)
    voter_threshold_notice = await h.wait_reply_text("user_b", "Remove vote threshold reached. Message removed.", user_b_copy)
    await h.wait_for(
        "user_b",
        lambda msg: msg.id == user_b_copy.id and "Message removed." in (
            msg.raw_text or ""),
        label="user_b tombstone after delete-vote threshold",
    )
    sender_removed = await h.wait_reply_text("user_a", "Your message was removed:", source)
    note = await h.wait_reply_text("owner", "Moderation", owner_copy)
    required = {"Punish", "Remove for mods", "Revert", "Ban"}
    labels = set(h.button_texts(note))
    if not required.issubset(labels):
        raise E2EFailure(f"Mod dialog buttons missing. Have {sorted(labels)}")
    if any(len(row) != 1 for row in (note.buttons or [])):
        raise E2EFailure("Mod dialog buttons are not stacked vertically.")
    return {
        "source": source,
        "owner_copy": owner_copy,
        "user_b_copy": user_b_copy,
        "note": note,
    }


async def scenario_delete_votes_dialog(h: Harness) -> None:
    await _delete_vote_flow(h, "deletevote")
    print("PASS delete-votes-dialog")


async def scenario_mod_dialog_revert(h: Harness) -> None:
    flow = await _delete_vote_flow(h, "mrev")
    note = flow["note"]
    await h.click_button("owner", note, "Revert")
    await h.wait_for(
        "owner",
        lambda msg: msg.id == note.id and "Status: reverted" in (
            msg.raw_text or ""),
        label="mod note reverted status",
    )
    await h.wait_text("user_a", "Moderators did not confirm wrongdoing")
    reversal = await h.wait_text("user_b", "Your remove vote was reversed by moderators.")
    h.assert_reply_to(reversal, flow["user_b_copy"], "Remove-vote reversal notice")
    print("PASS mod-dialog-revert")


async def scenario_mod_dialog_punish(h: Harness) -> None:
    flow = await _delete_vote_flow(h, "mconf")
    note = flow["note"]
    await h.click_button("owner", note, "Punish")
    await h.wait_for(
        "owner",
        lambda msg: msg.id == note.id and "Status: punished" in (
            msg.raw_text or ""),
        label="mod note punished status",
    )
    penalty = await h.wait_text("user_a", "Moderation confirmed. Penalty:")
    h.assert_reply_to(penalty, flow["source"], "Punishment notice")
    print("PASS mod-dialog-punish")


async def scenario_mod_dialog_remove_for_mods(h: Harness) -> None:
    flow = await _delete_vote_flow(h, "mrm")
    note = flow["note"]
    owner_copy = flow["owner_copy"]
    await h.click_button("owner", note, "Remove for mods")
    await h.wait_for(
        "owner",
        lambda msg: msg.id == owner_copy.id and "Message removed." in (
            msg.raw_text or ""),
        label="owner tombstone after remove-for-mods",
    )
    await h.wait_for(
        "owner",
        lambda msg: msg.id == note.id and "removed for mods" in (
            msg.raw_text or "").lower(),
        label="mod note removed-for-mods status",
    )
    updated_note = await h.wait_for(
        "owner",
        lambda msg: msg.id == note.id and "Punish" in h.button_texts(msg),
        label="mod note still has punish button after remove-for-mods",
    )
    await h.click_button("owner", updated_note, "Punish")
    penalty = await h.wait_text("user_a", "Moderation confirmed. Penalty:")
    h.assert_reply_to(penalty, flow["source"], "Punishment after remove-for-mods notice")
    print("PASS mod-dialog-remove-for-mods")


async def scenario_mod_dialog_ban_purge(h: Harness) -> None:
    flow = await _delete_vote_flow(h, "mban")
    note = flow["note"]
    second_marker = h.marker("ban-purge")
    await h.send_text("user_a", second_marker)
    await h.wait_text("owner", second_marker)
    second_b = await h.wait_text("user_b", second_marker)

    await h.click_button("owner", note, "Ban")
    await h.wait_for(
        "owner",
        lambda msg: msg.id == note.id and "Banned" in (msg.raw_text or "") and "Purged cached messages:" in (msg.raw_text or ""),
        label="mod note ban-purge action",
    )
    updated_note = await h.wait_for(
        "owner",
        lambda msg: msg.id == note.id and "Banned" in (msg.raw_text or ""),
        label="updated ban note",
    )
    if h.button_texts(updated_note):
        raise E2EFailure(f"Ban note still has action buttons: {h.button_texts(updated_note)}")
    if any(bad in (updated_note.raw_text or "") for bad in ("Voters:", "Actions:", "Banned sender")):
        raise E2EFailure(f"Ban note contains stale moderation noise: {updated_note.raw_text!r}")
    await h.wait_for(
        "user_b",
        lambda msg: msg.id == second_b.id and "Message removed." in (
            msg.raw_text or ""),
        label="second user_b copy tombstoned by ban purge",
    )
    banned = await h.wait_text("user_a", "You are banned.")
    h.assert_reply_to(banned, flow["source"], "Ban notice")
    print("PASS mod-dialog-ban-purge")


async def scenario_reply_info_contexts(h: Harness) -> None:
    await h.start_all()
    h.clear_inboxes()

    normal_marker = h.marker("info-normal")
    normal_source = await h.send_text("user_a", normal_marker)
    normal_owner = await h.wait_text("owner", normal_marker)
    await h.send_text("owner", "/info", reply_to=normal_owner)
    normal_info = await h.wait_reply_text("owner", "Role: user", normal_owner)
    if "Role: admin" in (normal_info.raw_text or ""):
        raise E2EFailure("/info on normal message returned caller/admin info.")

    forward_source = await _telegram_text_post(h)
    forward_marker = forward_source.raw_text.strip()[:40]
    await h.forward_to_bot("user_a", forward_source)
    forward_owner = await h.wait_text("owner", forward_marker)
    await h.send_text("owner", "/info", reply_to=forward_owner)
    await h.wait_reply_text("owner", "Role: user", forward_owner)

    owner_prompt = h.marker("info-whisper-target")
    await h.send_text("owner", owner_prompt)
    owner_prompt_for_a = await h.wait_text("user_a", owner_prompt)
    whisper_marker = h.marker("info-whisper")
    await h.send_text("user_a", f"/w {whisper_marker}", reply_to=owner_prompt_for_a)
    whisper_owner = await h.wait_text("owner", whisper_marker)
    await h.send_text("owner", "/info", reply_to=whisper_owner)
    await h.wait_reply_text("owner", "Role: user", whisper_owner)

    tomb_marker = h.marker("info-tombstone")
    await h.send_text("user_a", tomb_marker)
    tomb_owner = await h.wait_text("owner", tomb_marker)
    tomb_b = await h.wait_text("user_b", tomb_marker)
    await h.send_text("owner", "/delete", reply_to=tomb_owner)
    await h.wait_reply_text("owner", "Deleted (", tomb_owner)
    await h.wait_for(
        "user_b",
        lambda msg: msg.id == tomb_b.id and "Message removed." in (msg.raw_text or ""),
        label="user_b tombstone for /info",
    )
    await h.send_text("user_b", "/info", reply_to=tomb_b)
    await h.wait_reply_text("user_b", "Normal users cannot inspect others.", tomb_b)

    await h.send_text("owner", f"/mod {h.accounts['user_b'].user_id}")
    await h.wait_text("owner", "promoted to moderator")
    modwhisper_marker = h.marker("info-modwhisper")
    await h.send_text("owner", f"/wmods {modwhisper_marker}")
    modwhisper_b = await h.wait_text("user_b", modwhisper_marker)
    if "~ mods" in (modwhisper_b.raw_text or ""):
        raise E2EFailure("/wmods included the /modsay suffix.")
    await h.send_text("user_b", "/info", reply_to=modwhisper_b)
    await h.wait_reply_text("user_b", "Role: admin", modwhisper_b)
    print("PASS reply-info-contexts")


async def scenario_signed_media_identity(h: Harness) -> None:
    await h.start_all()
    h.clear_inboxes()
    await h.send_text("user_a", "/togglesign")
    await h.wait_text("user_a", "Persistent signing: on")
    image_path = h.runtime_path("signed-media.png")
    _make_test_image(image_path, variant="signed-media")
    await h.send_file("user_a", image_path)
    signed_media = await h.wait_for(
        "user_b",
        lambda msg: bool(getattr(msg, "photo", None)) and "@" in (msg.raw_text or ""),
        label="signed media identity caption",
    )
    if "~" in (signed_media.raw_text or ""):
        raise E2EFailure(f"Signed media identity should not include '~': {signed_media.raw_text!r}")
    print("PASS signed-media-identity")


async def scenario_mod_note_reply_context(h: Harness) -> None:
    await h.start_all()
    await h.send_text("owner", f"/mod {h.accounts['user_b'].user_id}")
    await h.wait_text("owner", "promoted to moderator")
    h.clear_inboxes()

    marker = h.marker("mod-note-reply")
    await h.send_text("user_a", marker)
    owner_copy = await h.wait_text("owner", marker)
    user_b_copy = await h.wait_text("user_b", marker)
    await h.send_text("user_b", "/deletevote", reply_to=user_b_copy)
    await h.wait_reply_text("user_b", "Remove vote recorded (1/2)", user_b_copy)
    await h.send_text("owner", "/deletevote", reply_to=owner_copy)
    await h.wait_reply_text("owner", "Remove vote threshold reached. Message removed.", owner_copy)
    owner_note = await h.wait_reply_text("owner", "Moderation", owner_copy)
    user_b_note = await h.wait_reply_text("user_b", "Moderation", user_b_copy)

    reply_marker = h.marker("mod-note-thread")
    await h.send_text("owner", reply_marker, reply_to=owner_note)
    mod_reply = await h.wait_reply_text("user_b", reply_marker, user_b_note)
    if not mod_reply:
        raise E2EFailure("Mod note reply was not delivered to mod thread.")
    await h.assert_absent_text("user_a", reply_marker)
    print("PASS mod-note-reply-context")


async def scenario_delete_flow_parity(h: Harness) -> None:
    await h.start_all()
    await h.send_text("owner", f"/mod {h.accounts['user_b'].user_id}")
    await h.wait_text("owner", "promoted to moderator")
    h.clear_inboxes()

    regular_marker = h.marker("delete-parity-regular")
    regular_source = await h.send_text("user_a", regular_marker)
    regular_mod_copy = await h.wait_text("user_b", regular_marker)
    regular_owner_copy = await h.wait_text("owner", regular_marker)
    await _assert_direct_delete_flow(
        h,
        actor="user_b",
        target=regular_mod_copy,
        sender="user_a",
        sender_source=regular_source,
        marker=regular_marker,
        label="regular message",
        marked_targets=[("user_b", regular_mod_copy), ("owner", regular_owner_copy)],
        mod_note_targets=[("user_b", regular_mod_copy), ("owner", regular_owner_copy)],
        no_moderation_accounts=["user_a"],
    )

    whisper_target = h.marker("delete-parity-whisper-target")
    await h.send_text("user_b", whisper_target)
    prompt_for_a = await h.wait_text("user_a", whisper_target)
    whisper_marker = h.marker("delete-parity-whisper")
    whisper_source = await h.send_text("user_a", f"/w {whisper_marker}", reply_to=prompt_for_a)
    whisper_mod_copy = await h.wait_text("user_b", whisper_marker)
    whisper_owner_copy = await h.wait_text("owner", whisper_marker)
    await _assert_direct_delete_flow(
        h,
        actor="user_b",
        target=whisper_mod_copy,
        sender="user_a",
        sender_source=whisper_source,
        marker=whisper_marker,
        label="whisper",
        marked_targets=[("user_b", whisper_mod_copy), ("owner", whisper_owner_copy)],
        mod_note_targets=[("user_b", whisper_mod_copy), ("owner", whisper_owner_copy)],
        no_moderation_accounts=["user_a"],
    )

    modwhisper_marker = h.marker("delete-parity-modwhisper")
    modwhisper_source = await h.send_text("owner", f"/wmods {modwhisper_marker}")
    modwhisper_mod_copy = await h.wait_text("user_b", modwhisper_marker)
    await _assert_direct_delete_flow(
        h,
        actor="user_b",
        target=modwhisper_mod_copy,
        sender="owner",
        sender_source=modwhisper_source,
        marker=modwhisper_marker,
        label="modwhisper",
        marked_targets=[("user_b", modwhisper_mod_copy)],
        mod_note_targets=[("user_b", modwhisper_mod_copy)],
        no_moderation_accounts=["owner", "user_a"],
    )

    user_modwhisper_marker = h.marker("delete-parity-user-modwhisper")
    user_modwhisper_source = await h.send_text("user_a", f"/wmods {user_modwhisper_marker}")
    user_modwhisper_mod_copy = await h.wait_text("user_b", user_modwhisper_marker)
    user_modwhisper_owner_copy = await h.wait_text("owner", user_modwhisper_marker)
    await _assert_direct_delete_flow(
        h,
        actor="user_b",
        target=user_modwhisper_mod_copy,
        sender="user_a",
        sender_source=user_modwhisper_source,
        marker=user_modwhisper_marker,
        label="regular-user modwhisper",
        marked_targets=[("user_b", user_modwhisper_mod_copy), ("owner", user_modwhisper_owner_copy)],
        mod_note_targets=[("user_b", user_modwhisper_mod_copy), ("owner", user_modwhisper_owner_copy)],
        no_moderation_accounts=["user_a"],
    )

    reaction_modwhisper_marker = h.marker("delete-parity-reaction-user-modwhisper")
    reaction_modwhisper_source = await h.send_text("user_a", f"/wmods {reaction_modwhisper_marker}")
    reaction_modwhisper_mod_copy = await h.wait_text("user_b", reaction_modwhisper_marker)
    reaction_modwhisper_owner_copy = await h.wait_text("owner", reaction_modwhisper_marker)
    await _assert_reaction_delete_flow(
        h,
        actor="user_b",
        target=reaction_modwhisper_mod_copy,
        sender="user_a",
        sender_source=reaction_modwhisper_source,
        marker=reaction_modwhisper_marker,
        label="regular-user modwhisper reaction",
        marked_targets=[("user_b", reaction_modwhisper_mod_copy), ("owner", reaction_modwhisper_owner_copy)],
        mod_note_targets=[("user_b", reaction_modwhisper_mod_copy), ("owner", reaction_modwhisper_owner_copy)],
        no_moderation_accounts=["user_a"],
    )
    print("PASS delete-flow-parity")


async def _assert_direct_delete_flow(
    h: Harness,
    *,
    actor: str,
    target: Any,
    sender: str,
    sender_source: Any,
    marker: str,
    label: str,
    marked_targets: list[tuple[str, Any]],
    mod_note_targets: list[tuple[str, Any]],
    no_moderation_accounts: list[str],
) -> None:
    h.clear_inboxes()
    await h.send_text(actor, "/delete", reply_to=target)
    deleted = await h.wait_reply_text(actor, "Deleted (", target)
    h.assert_reply_to(deleted, target, f"{label} delete acknowledgement")
    notes: dict[str, Any] = {}
    for account_name, note_target in mod_note_targets:
        note = await h.wait_reply_text(account_name, "Moderation", note_target)
        h.assert_reply_to(note, note_target, f"{label} {account_name} moderation note")
        _assert_delete_mod_note_shape(h, note, account_name=account_name, label=f"{label} {account_name}")
        notes[account_name] = note
    for account_name in no_moderation_accounts:
        await h.assert_absent_text(account_name, "Moderation", timeout=2)
        _assert_no_moderation_buttons(h, account_name, label=label)
    await h.assert_absent_text(actor, "Confirm moderator delete", timeout=2)
    current_target = await h.fetch_bot_message(actor, target)
    if not current_target or marker not in (current_target.raw_text or ""):
        raise E2EFailure(f"{label} mod-visible copy disappeared or changed: {getattr(current_target, 'raw_text', None)!r}")
    if "Message removed." in (current_target.raw_text or ""):
        raise E2EFailure(f"{label} mod-visible copy was tombstoned instead of left available.")
    sender_notice = await h.wait_reply_text(sender, "Your message was removed:", sender_source)
    h.assert_reply_to(sender_notice, sender_source, f"{label} sender removal notice")
    for account_name, marked_target in marked_targets:
        await h.wait_reaction(account_name, marked_target, "✍️", timeout=20)
    actor_note = notes.get(actor)
    if actor_note:
        before_ids = {
            msg.id
            for msg in h.history[actor]
            if _message_id(msg) is not None and "Moderation" in (msg.raw_text or "")
        }
        await h.click_button(actor, actor_note, "Remove for mods")
        updated_note = await h.wait_for(
            actor,
            lambda msg: msg.id == actor_note.id and "removed for mods" in (msg.raw_text or "").lower(),
            label=f"{label} existing note updated after remove-for-mods",
        )
        labels = h.button_texts(updated_note)
        if labels != ["Punish"]:
            raise E2EFailure(f"{label} remove-for-mods note buttons differ: {labels!r}")
        after_ids = {
            msg.id
            for msg in h.history[actor]
            if _message_id(msg) is not None and "Moderation" in (msg.raw_text or "")
        }
        if after_ids - before_ids:
            raise E2EFailure(f"{label} remove-for-mods created a new moderation note: before={before_ids}, after={after_ids}")
        for account_name in no_moderation_accounts:
            await h.assert_absent_text(account_name, "Moderation", timeout=2)
            _assert_no_moderation_buttons(h, account_name, label=f"{label} after remove-for-mods")


def _assert_delete_mod_note_shape(h: Harness, note: Any, *, account_name: str, label: str) -> None:
    raw_note = note.raw_text or ""
    for required in ("Moderation", "Sender:", "Reason: deleted by moderator"):
        if required not in raw_note:
            raise E2EFailure(f"{label} moderation note missing {required!r}: {raw_note!r}")
    if "Status:" in raw_note:
        raise E2EFailure(f"{label} direct delete note should not include status noise: {raw_note!r}")
    if "Whisper" in raw_note:
        raise E2EFailure(f"{label} moderation note should not use whisper-specific wording: {raw_note!r}")
    labels = h.button_texts(note)
    expected = ["Punish", "Remove for mods", "Ban"] if account_name == "owner" else ["Punish", "Remove for mods"]
    if labels != expected:
        raise E2EFailure(f"{label} moderation note buttons differ from regular delete flow: {labels!r}")


async def _assert_reaction_delete_flow(
    h: Harness,
    *,
    actor: str,
    target: Any,
    sender: str,
    sender_source: Any,
    marker: str,
    label: str,
    marked_targets: list[tuple[str, Any]],
    mod_note_targets: list[tuple[str, Any]],
    no_moderation_accounts: list[str],
) -> None:
    h.clear_inboxes()
    await h.send_reaction(actor, target, "✍️")
    notes: dict[str, Any] = {}
    for account_name, note_target in mod_note_targets:
        note = await h.wait_reply_text(account_name, "Moderation", note_target)
        h.assert_reply_to(note, note_target, f"{label} {account_name} moderation note")
        _assert_delete_mod_note_shape(h, note, account_name=account_name, label=f"{label} {account_name}")
        notes[account_name] = note
    for account_name in no_moderation_accounts:
        await h.assert_absent_text(account_name, "Moderation", timeout=2)
        _assert_no_moderation_buttons(h, account_name, label=label)
    current_target = await h.fetch_bot_message(actor, target)
    if not current_target or marker not in (current_target.raw_text or ""):
        raise E2EFailure(f"{label} mod-visible copy disappeared or changed: {getattr(current_target, 'raw_text', None)!r}")
    sender_notice = await h.wait_reply_text(sender, "Your message was removed:", sender_source)
    h.assert_reply_to(sender_notice, sender_source, f"{label} sender removal notice")
    for account_name, marked_target in marked_targets:
        await h.wait_reaction(account_name, marked_target, "✍️", timeout=20)
    actor_note = notes.get(actor)
    if actor_note:
        await h.click_button(actor, actor_note, "Remove for mods")
        await h.wait_for(
            actor,
            lambda msg: msg.id == actor_note.id and "removed for mods" in (msg.raw_text or "").lower(),
            label=f"{label} existing note updated after remove-for-mods",
        )
        for account_name in no_moderation_accounts:
            await h.assert_absent_text(account_name, "Moderation", timeout=2)
            _assert_no_moderation_buttons(h, account_name, label=f"{label} after remove-for-mods")


def _assert_no_moderation_buttons(h: Harness, account_name: str, *, label: str) -> None:
    forbidden = {"Punish", "Remove for mods", "Ban", "Revert"}
    for msg in h.history[account_name]:
        labels = set(h.button_texts(msg))
        leaked = labels & forbidden
        if leaked:
            raise E2EFailure(f"{label} leaked moderation buttons to {account_name}: {sorted(leaked)} on {msg.raw_text!r}")


async def scenario_media_tombstone(h: Harness) -> None:
    await h.start_all()
    h.clear_inboxes()
    image_path = h.runtime_path("media-tombstone.png")
    marker = h.marker("media-tombstone")
    _make_test_image(image_path, variant=marker)
    await h.send_file("user_a", image_path, caption=marker)
    owner_copy = await h.wait_text("owner", marker)
    user_b_copy = await h.wait_text("user_b", marker)

    await h.send_text("user_b", "/deletevote", reply_to=user_b_copy)
    first_vote = await h.wait_reply_text("user_b", "Remove vote recorded (1/2)", user_b_copy)
    await h.send_text("owner", "/deletevote", reply_to=owner_copy)
    threshold_vote = await h.wait_reply_text("owner", "Remove vote threshold reached. Message removed.", owner_copy)
    voter_threshold_notice = await h.wait_reply_text("user_b", "Remove vote threshold reached. Message removed.", user_b_copy)
    tombstone = await h.wait_for(
        "user_b",
        lambda msg: msg.id == user_b_copy.id and "Message removed." in (
            msg.raw_text or ""),
        label="media replacement tombstone on same user_b message",
    )
    if tombstone.id != user_b_copy.id:
        raise E2EFailure("Media tombstone was not applied to the original Telegram message.")
    print("PASS media-tombstone")


async def scenario_forward_preservation(h: Harness) -> None:
    await h.start_all()
    h.clear_inboxes()
    source = await _telegram_text_post(h)
    marker = source.raw_text.strip()[:40]
    await h.forward_to_bot("user_a", source)
    forwarded = await h.wait_text("user_b", marker)
    if not getattr(forwarded, "fwd_from", None):
        raise E2EFailure("Recipient copy was not delivered as a Telegram forward.")
    print("PASS forward-preservation")


async def scenario_forward_delete_removes_uneditable_forward(h: Harness) -> None:
    await h.start_all()
    h.clear_inboxes()
    source = await _telegram_text_post(h)
    marker = source.raw_text.strip()[:40]
    await h.forward_to_bot("user_a", source)
    owner_forwarded = await h.wait_text("owner", marker)
    user_b_forwarded = await h.wait_text("user_b", marker)
    if not getattr(user_b_forwarded, "fwd_from", None):
        raise E2EFailure("Recipient copy was not delivered as a Telegram forward.")
    await h.send_text("owner", "/delete", reply_to=owner_forwarded)
    await h.wait_reply_text("owner", "Deleted (", owner_forwarded)
    await h.wait_deleted("user_b", user_b_forwarded)
    print("PASS forward-delete-removes-uneditable-forward")


async def _telegram_text_post(h: Harness):
    account = h.accounts["user_a"]
    for candidate in await account.client.get_messages("Telegram", limit=20):
        if candidate.raw_text and len(candidate.raw_text.strip()) >= 20:
            return candidate
    raise E2EFailure("Could not find a text post in @Telegram to forward.")


SCENARIOS: dict[str, Callable[[Harness], Awaitable[None]]] = {
    "basic": scenario_basic,
    "duplicate-media": scenario_duplicate_media,
    "moderation": scenario_moderation,
    "reactions": scenario_reactions,
    "delete-votes-dialog": scenario_delete_votes_dialog,
    "mod-dialog-revert": scenario_mod_dialog_revert,
    "mod-dialog-punish": scenario_mod_dialog_punish,
    "mod-dialog-remove-for-mods": scenario_mod_dialog_remove_for_mods,
    "media-tombstone": scenario_media_tombstone,
    "forward-preservation": scenario_forward_preservation,
    "forward-delete-removes-uneditable-forward": scenario_forward_delete_removes_uneditable_forward,
    "mod-dialog-ban-purge": scenario_mod_dialog_ban_purge,
    "reply-info-contexts": scenario_reply_info_contexts,
    "signed-media-identity": scenario_signed_media_identity,
    "mod-note-reply-context": scenario_mod_note_reply_context,
    "delete-flow-parity": scenario_delete_flow_parity,
}


async def login(config_path: Path) -> None:
    h = Harness(config_path, allow_login_prompt=True)
    await h.connect_accounts()
    for account in h.accounts.values():
        account.client.disconnect()


async def run_scenario(config_path: Path, scenario: str) -> None:
    async with Harness(config_path) as h:
        if scenario == "all":
            for name, fn in SCENARIOS.items():
                print(f"RUN {name}")
                await fn(h)
        else:
            await SCENARIOS[scenario](h)


def _make_test_image(path: Path, *, variant: str = "default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = sum(ord(ch) for ch in variant)
    base = ((36 + seed) % 255, (111 + seed * 3) % 255, (184 + seed * 7) % 255)
    accent = ((230 + seed * 5) % 255, (230 + seed * 11) % 255, (80 + seed * 13) % 255)
    img = Image.new("RGB", (96, 96), base)
    for x in range(96):
        for y in range(96):
            if (x + y) % 11 == 0:
                img.putpixel((x, y), accent)
    img.save(path, format="PNG")


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (ROOT / path).resolve()


def _bot_username(value: str) -> str:
    return value if value.startswith("@") else f"@{value}"


def _get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _require(data: dict[str, Any], dotted: str) -> Any:
    value = _get(data, dotted)
    if value in (None, ""):
        raise E2EFailure(f"Missing required config value: {dotted}")
    return value


def _message_id(message: Any) -> int | None:
    value = getattr(message, "id", None)
    if value is not None:
        return int(value)
    nested = getattr(message, "message", None)
    value = getattr(nested, "id", None)
    return int(value) if value is not None else None


def _message_has_reaction(message: Any, emoji: str) -> bool:
    expected = _normalize_emoji(emoji)
    reactions = getattr(message, "reactions", None)
    if not reactions:
        return False
    for result in getattr(reactions, "results", []) or []:
        if _normalize_emoji(_reaction_emoji(getattr(result, "reaction", None))) == expected:
            return True
    for recent in getattr(reactions, "recent_reactions", []) or []:
        if _normalize_emoji(_reaction_emoji(getattr(recent, "reaction", None))) == expected:
            return True
    return False


def _reaction_emoji(reaction: Any) -> str | None:
    if reaction is None:
        return None
    value = getattr(reaction, "emoticon", None) or getattr(reaction, "emoji", None)
    return str(value) if value else None


def _normalize_emoji(value: str | None) -> str | None:
    return value.replace("\ufe0f", "") if value else value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Telegram E2E harness for forward-bot.")
    parser.add_argument("--config", default="e2e/telethon_config.yml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Create/refresh Telethon user sessions.")
    run = sub.add_parser("run", help="Run one E2E scenario.")
    run.add_argument("scenario", choices=["all", *SCENARIOS.keys()])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = _resolve_path(args.config)
    if not config_path.exists():
        print(f"Missing config file: {config_path}", file=sys.stderr)
        print("Copy e2e/telethon_config.example.yml to e2e/telethon_config.yml first.", file=sys.stderr)
        return 2
    try:
        if args.command == "login":
            asyncio.run(login(config_path))
        elif args.command == "run":
            asyncio.run(run_scenario(config_path, args.scenario))
        return 0
    except E2EFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
