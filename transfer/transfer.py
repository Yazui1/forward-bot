#!/usr/bin/env python3
"""Automated recovery service for the Forward Bot fleet."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from telethon import TelegramClient, events, functions, types
from telethon.errors import RPCError
from telethon.sessions import StringSession


HERE = Path(__file__).resolve().parent
SETTINGS_PATH = HERE / "config.yml"
BOTFATHER = "BotFather"
ABUSE_NOTIFICATION = "AbuseNotification"
ANONYMOUS_BOT = "@IncogNoteBot"
TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
BOT_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,})\b")
URL_RE = re.compile(r"https://t\.me/IncogNoteBot\?start=[A-Za-z0-9_-]+")
OWNERSHIP_PHRASE = "ownership of the bot"


@dataclass(frozen=True)
class Account:
    username: str
    session: Path


@dataclass(frozen=True)
class BotMapping:
    handle_prefix: str
    friendly_name: str
    config_path: Path
    token_path: str
    restart_command: tuple[str, ...]
    handle: str | None = None


@dataclass
class Recovery:
    key: str
    old_handle: str
    receiver_username: str
    anonymous_link: str
    stage: str = "waiting_submission"
    new_handle: str | None = None
    anonymous_message_id: int | None = None


class RecoveryError(RuntimeError):
    pass


class Config:
    def __init__(self, value: dict[str, Any], source: Path) -> None:
        self.value = value
        self.source = source
        self.telegram = required_mapping(value, "telegram")
        self.announcement = required_mapping(value, "announcement")
        self.cloudflare = required_mapping(value, "cloudflare")
        self.main = account_from(self.telegram, "main", source.parent)
        self.pool = tuple(account_from(item, None, source.parent) for item in optional_list(self.telegram, "pool"))
        self.api_id = int(required_string(self.telegram, "api_id"))
        self.api_hash = required_string(self.telegram, "api_hash")
        self.announcement_channel = required_string(self.announcement, "channel")
        self.recovery_template = required_string(self.announcement, "recovery_template")
        self.restored_template = required_string(self.announcement, "restored_template")
        self.state_path = resolve_path(value.get("state_path", ".state/recovery.json"), source.parent)
        self.snapshot_dir = resolve_path(value.get("snapshot_dir", ".state/snapshots"), source.parent)
        self.bots = tuple(self._load_bots())
        prefixes = [item.handle_prefix.casefold() for item in self.bots]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("Every bots.handle_prefix must be unique.")

    def _load_bots(self) -> list[BotMapping]:
        result: list[BotMapping] = []
        for item in required_list(self.value, "bots"):
            if not isinstance(item, dict):
                raise ValueError("Every bots entry must be a YAML mapping.")
            command = item.get("restart_command")
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
                raise ValueError("bots.restart_command must be a non-empty command string or list.")
            handle_prefix = required_string(item, "handle_prefix").lstrip("@")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", handle_prefix) is None:
                raise ValueError(
                    "bots.handle_prefix must contain only Telegram username characters."
                )
            handle = (
                normalize_username(str(item["handle"]))
                if item.get("handle")
                else None
            )
            if handle and (
                BOT_USERNAME_RE.fullmatch(handle) is None
                or not handle.casefold().endswith("bot")
            ):
                raise ValueError(
                    f"bots.handle {handle} must be a valid Telegram bot username."
                )
            result.append(BotMapping(
                handle_prefix=handle_prefix,
                friendly_name=required_string(item, "friendly_name"),
                config_path=resolve_path(required_string(item, "config_path"), self.source.parent),
                token_path=required_string(item, "token_path"),
                restart_command=tuple(command),
                handle=handle,
            ))
        if not result:
            raise ValueError("bots must contain at least one configured bot.")
        return result

    def bot_for_handle(self, handle: str) -> BotMapping | None:
        clean = handle.lstrip("@").casefold()
        matches = [item for item in self.bots if clean.startswith(item.handle_prefix.casefold())]
        if len(matches) > 1:
            raise RecoveryError(f"More than one mapping matches @{clean}.")
        return matches[0] if matches else None

def required_mapping(value: dict[str, Any], key: str | None) -> dict[str, Any]:
    item = value.get(key) if key else value
    if not isinstance(item, dict):
        raise ValueError(f"{key} must be a YAML mapping.")
    return item


def required_list(value: dict[str, Any], key: str) -> list[Any]:
    item = value.get(key)
    if not isinstance(item, list):
        raise ValueError(f"{key} must be a YAML list.")
    return item


def optional_list(value: dict[str, Any], key: str) -> list[Any]:
    item = value.get(key)
    if item is None:
        return []
    if not isinstance(item, list):
        raise ValueError(f"{key} must be a YAML list or empty.")
    return item


def required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return item.strip()


def resolve_path(value: object, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base / path


def normalize_username(value: str) -> str:
    value = value.strip().lstrip("@")
    if not value:
        raise ValueError("A Telegram username is required.")
    return "@" + value


def account_from(value: dict[str, Any], key: str | None, base: Path) -> Account:
    item = required_mapping(value, key)
    return Account(normalize_username(required_string(item, "username")), resolve_path(required_string(item, "session"), base))


def response_text(message: object) -> str:
    return str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")


def message_handles(message: object) -> set[str]:
    return {"@" + match.group(1) for match in BOT_USERNAME_RE.finditer(response_text(message))}


def format_template(template: str, **values: str) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:
        raise RecoveryError(f"Unknown field in announcement template: {exc.args[0]}") from exc


class Service:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.main_client = make_client(config.main, config)
        self.pool_clients = {account.username.casefold(): make_client(account, config) for account in config.pool}
        self.recoveries: dict[str, Recovery] = {}
        self.lock = asyncio.Lock()
        self.log = logging.getLogger("recovery")

    async def run(self) -> None:
        self._load_state()
        self._register_handlers()
        await self._start_client(self.main_client, self.config.main)
        await self._ensure_snapshots()
        for account in self.config.pool:
            try:
                await self._start_client(
                    self.pool_clients[account.username.casefold()], account
                )
            except (OSError, RPCError, RecoveryError) as exc:
                self.log.error("Pool account %s is unavailable: %s", account.username, exc)
        await self._check_configured_bots()
        await self._resume_recoveries()
        self.log.info("Recovery service started as %s", self.config.main.username)
        await asyncio.gather(self.main_client.run_until_disconnected(), *(client.run_until_disconnected() for client in self.pool_clients.values()))

    async def _ensure_snapshots(self) -> None:
        for mapping in self.config.bots:
            path = snapshot_metadata_path(self.config, mapping)
            if path.exists():
                load_snapshot(self.config, mapping)
                continue
            if mapping.handle is None:
                raise RecoveryError(
                    f"No snapshot exists for {mapping.friendly_name}; configure its handle "
                    "for the first service start."
                )
            await snapshot_bot(
                self.main_client,
                self.config.api_id,
                self.config.api_hash,
                self.config,
                mapping,
            )
            self.log.info("Created settings snapshot for %s", mapping.friendly_name)

    async def _check_configured_bots(self) -> None:
        for mapping in self.config.bots:
            if mapping.handle is None:
                continue
            if await bot_needs_recovery(self.main_client, mapping.handle):
                self.log.warning(
                    "Configured bot %s (%s) is unavailable; starting recovery.",
                    mapping.friendly_name,
                    mapping.handle,
                )
                await self._start_recovery_for_mapping(mapping, mapping.handle)

    async def _start_client(self, client: TelegramClient, account: Account) -> None:
        account.session.parent.mkdir(parents=True, exist_ok=True)
        await client.start()
        me = await client.get_me()
        actual = normalize_username(me.username or "")
        if actual.casefold() != account.username.casefold():
            raise RecoveryError(f"Session {account.session} belongs to {actual}, expected {account.username}.")

    def _register_handlers(self) -> None:
        self.main_client.add_event_handler(self._on_main_message, events.NewMessage(incoming=True))
        for username, client in self.pool_clients.items():
            client.add_event_handler(lambda event, pool=username: self._on_pool_message(pool, event), events.NewMessage(incoming=True))

    async def _on_main_message(self, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        username = str(getattr(sender, "username", "") or "").casefold()
        if username == ABUSE_NOTIFICATION.casefold():
            await self._start_recovery(event.message)
        elif username == ANONYMOUS_BOT.lstrip("@").casefold():
            await self._on_anonymous_message(
                self.config.main.username.casefold(), self.main_client, event
            )
        elif username == BOTFATHER.casefold() and OWNERSHIP_PHRASE in response_text(event.message).casefold():
            await self._on_main_ownership(event.message)

    async def _on_pool_message(self, pool: str, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        username = str(getattr(sender, "username", "") or "").casefold()
        if username == ANONYMOUS_BOT.lstrip("@").casefold():
            await self._on_anonymous_message(pool, self.pool_clients[pool], event)
            return
        if username != BOTFATHER.casefold() or OWNERSHIP_PHRASE not in response_text(event.message).casefold():
            return
        async with self.lock:
            pending = next((item for item in self.recoveries.values() if item.receiver_username.casefold() == pool and item.stage == "waiting_pool"), None)
            if pending is None:
                return
            if pending.new_handle not in message_handles(event.message):
                self.log.warning("Ignoring unmatched pool ownership message: %s", response_text(event.message))
                return
            pending.stage = "transferring"
            self._save_state()
        try:
            await self._transfer_to_main(pending, pool)
        except Exception:
            self.log.exception("Could not transfer %s from pool %s to main", pending.new_handle, pool)

    async def _on_anonymous_message(
        self, receiver: str, client: TelegramClient, event: events.NewMessage.Event
    ) -> None:
        if not any("block" in label.casefold() for label in button_labels(event.message)):
            return
        error_message: str | None = None
        async with self.lock:
            pending = next((
                item for item in self.recoveries.values()
                if item.receiver_username.casefold() == receiver
                and item.stage == "waiting_submission"
            ), None)
            if pending is None:
                return
            mapping = self._mapping(pending)
            valid = [
                handle for handle in message_handles(event.message)
                if self._valid_replacement(mapping, handle)
            ]
            if len(valid) != 1:
                error_message = (
                    f"Your message does not contain a valid bot. Create a bot named "
                    f"{mapping.handle_prefix}<a random short number>bot and reply to this message with its @username here."
                )
            else:
                candidate = valid[0]
        if error_message:
            await event.reply(error_message)
            return
        try:
            entity = await client.get_entity(candidate)
            if not getattr(entity, "bot", False):
                raise ValueError(f"{candidate} is not a Telegram bot.")
            await client.send_message(candidate, "/start")
        except (asyncio.TimeoutError, OSError, RPCError, ValueError):
            await event.reply(
                f"Your message does not contain a valid bot. Create a bot named "
                f"{mapping.handle_prefix}<a random short number>bot and reply to this message with its @username here."
            )
            return
        async with self.lock:
            if pending.stage != "waiting_submission":
                return
            pending.new_handle = candidate
            pending.anonymous_message_id = event.message.id
            pending.stage = (
                "waiting_main"
                if receiver == self.config.main.username.casefold()
                else "waiting_pool"
            )
            self._save_state()
        await event.reply(
            f"Thank you. Transfer {candidate} to "
            f"{self._receiver_display_name(receiver)}."
        )

    async def _start_recovery(self, message: object) -> None:
        handles = message_handles(message)
        if not handles:
            self.log.warning("Abuse notification had no bot handle: %s", response_text(message))
            return
        for old_handle in handles:
            mapping = self.config.bot_for_handle(old_handle)
            if mapping is not None:
                await self._start_recovery_for_mapping(mapping, old_handle)

    async def _start_recovery_for_mapping(
        self, mapping: BotMapping, old_handle: str
    ) -> None:
        async with self.lock:
            key = mapping.handle_prefix.casefold()
            if key in self.recoveries:
                self.log.info("Recovery for %s is already active", mapping.friendly_name)
                return
            receiver = self._available_receiver()
            if receiver is None:
                self.log.error("No available receiving account for %s", mapping.friendly_name)
                return
            link = await anonymous_link(self._client_for(receiver), ANONYMOUS_BOT)
            recovery = Recovery(key, old_handle, receiver, link)
            self.recoveries[key] = recovery
            self._save_state()
            await self.main_client.send_message(self.config.announcement_channel, format_template(
                self.config.recovery_template, friendly_name=mapping.friendly_name,
                old_handle=old_handle, handle_prefix=mapping.handle_prefix, anonymous_link=link,
            ))
            self.log.info("Requested replacement for %s", mapping.friendly_name)

    async def _on_main_ownership(self, message: object) -> None:
        handles = message_handles(message)
        async with self.lock:
            recovery = next((item for item in self.recoveries.values() if item.stage in {"transferring", "waiting_main"} and item.new_handle in handles), None)
            if recovery is None:
                return
            recovery.stage = "finalizing"
            self._save_state()
        try:
            await self._finalize(recovery)
        except Exception:
            self.log.exception("Recovery finalization failed for %s", recovery.new_handle)
            async with self.lock:
                self._save_state()

    async def _transfer_to_main(self, recovery: Recovery, pool: str) -> None:
        assert recovery.new_handle
        await self.main_client.send_message(recovery.new_handle, "/start")
        await transfer_ownership(
            self.pool_clients[pool], recovery.new_handle, self.config.main.username
        )
        async with self.lock:
            if self.recoveries.get(recovery.key) is recovery and recovery.stage == "transferring":
                recovery.stage = "waiting_main"
                self._save_state()
        self.log.info("Transferred %s from %s to main account", recovery.new_handle, pool)

    async def _resume_recoveries(self) -> None:
        resumable_stages = {
            "transferring",
            "waiting_main",
            "finalizing",
            "finalizing_restore",
            "finalizing_about",
            "finalizing_config",
            "finalizing_publish",
            "finalizing_announce",
            "finalizing_cleanup",
            "finalizing_revoke",
        }
        for recovery in list(self.recoveries.values()):
            # Passive stages continue through the event handlers after startup.
            # These stages mean an operation was already in progress when the
            # service stopped, so resume the finalization flow immediately.
            if recovery.stage not in resumable_stages:
                continue
            try:
                if recovery.stage == "transferring":
                    await self._transfer_to_main(
                        recovery, recovery.receiver_username.casefold()
                    )
                    continue
                await self._finalize(recovery)
            except Exception:
                self.log.exception(
                    "Could not resume recovery for %s; it remains at stage %s",
                    recovery.new_handle,
                    recovery.stage,
                )
                async with self.lock:
                    if self.recoveries.get(recovery.key) is recovery:
                        self._save_state()

    async def _finalize(self, recovery: Recovery) -> None:
        mapping = self._mapping(recovery)
        assert recovery.new_handle

        async def checkpoint(stage: str) -> None:
            async with self.lock:
                recovery.stage = stage
                self._save_state()

        if recovery.stage in {"waiting_main", "finalizing"}:
            await checkpoint("finalizing_restore")

        if recovery.stage == "finalizing_restore":
            token = await restore_bot_snapshot(
                self.main_client,
                self.config,
                mapping,
                recovery.old_handle,
                recovery.new_handle,
            )
            write_yaml_value(mapping.config_path, mapping.token_path, token)
            restart(mapping.restart_command)
            await asyncio.sleep(10)
            await checkpoint("finalizing_about")

        if recovery.stage == "finalizing_about":
            await update_runtime_about(
                self.main_client,
                recovery.new_handle,
                recovery.old_handle,
            )
            await checkpoint("finalizing_config")

        if recovery.stage == "finalizing_config":
            update_configured_handle(self.config, mapping, recovery.new_handle)
            await checkpoint("finalizing_publish")

        if recovery.stage == "finalizing_publish":
            await publish_page(self.config, mapping, recovery.new_handle)
            await checkpoint("finalizing_announce")

        if recovery.stage == "finalizing_announce":
            await self.main_client.send_message(self.config.announcement_channel, format_template(
                self.config.restored_template, friendly_name=mapping.friendly_name,
                new_handle=recovery.new_handle, old_handle=recovery.old_handle))
            await checkpoint("finalizing_cleanup")

        if recovery.stage == "finalizing_cleanup":
            anonymous_client = self._client_for(recovery.receiver_username.casefold())
            if recovery.anonymous_message_id is not None:
                await anonymous_client.send_message(
                    ANONYMOUS_BOT,
                    f"Thank you. {mapping.friendly_name} has been restored as {recovery.new_handle}.",
                    reply_to=recovery.anonymous_message_id,
                )
            await checkpoint("finalizing_revoke")

        if recovery.stage == "finalizing_revoke":
            anonymous_client = self._client_for(recovery.receiver_username.casefold())
            await revoke_anonymous_link(anonymous_client, ANONYMOUS_BOT)
            async with self.lock:
                self.recoveries.pop(recovery.key, None)
                self._save_state()
            self.log.info("Recovery complete for %s", mapping.friendly_name)

    def _mapping(self, recovery: Recovery) -> BotMapping:
        mapping = next((item for item in self.config.bots if item.handle_prefix.casefold() == recovery.key), None)
        if mapping is None:
            raise RecoveryError(f"Mapping {recovery.key} is no longer configured.")
        return mapping

    def _available_receiver(self) -> str | None:
        occupied = {item.receiver_username.casefold() for item in self.recoveries.values()}
        if not self.config.pool:
            main = self.config.main.username.casefold()
            return main if main not in occupied and self.main_client.is_connected() else None
        for account in self.config.pool:
            key = account.username.casefold()
            if key not in occupied and self.pool_clients[key].is_connected():
                return key
        return None

    def _client_for(self, username: str) -> TelegramClient:
        if username.casefold() == self.config.main.username.casefold():
            return self.main_client
        return self.pool_clients[username.casefold()]

    def _receiver_display_name(self, username: str) -> str:
        if username.casefold() == self.config.main.username.casefold():
            return self.config.main.username
        account = next(
            item for item in self.config.pool
            if item.username.casefold() == username.casefold()
        )
        return account.username

    @staticmethod
    def _valid_replacement(mapping: BotMapping, handle: str) -> bool:
        clean = handle.lstrip("@").casefold()
        return re.fullmatch(
            re.escape(mapping.handle_prefix.casefold()) + r"\d+bot", clean
        ) is not None

    def _load_state(self) -> None:
        if not self.config.state_path.exists():
            return
        try:
            loaded = json.loads(self.config.state_path.read_text(encoding="utf-8"))
            recoveries = []
            for item in loaded.get("recoveries", []):
                if "pool_username" in item and "receiver_username" not in item:
                    item["receiver_username"] = item.pop("pool_username")
                recoveries.append(Recovery(**item))
            self.recoveries = {item.key: item for item in recoveries}
        except (OSError, ValueError, TypeError) as exc:
            raise RecoveryError(f"Could not load recovery state: {exc}") from exc

    def _save_state(self) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"recoveries": [asdict(item) for item in self.recoveries.values()]}, indent=2)
        temporary = self.config.state_path.with_suffix(".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(self.config.state_path)
        try:
            os.chmod(self.config.state_path, 0o600)
        except OSError:
            pass


def make_client(account: Account, config: Config) -> TelegramClient:
    return TelegramClient(str(account.session), config.api_id, config.api_hash)


async def anonymous_link(client: TelegramClient, anonymous_bot: str) -> str:
    async with client.conversation(anonymous_bot, timeout=45, exclusive=True) as conv:
        await conv.send_message("/start")
        await conv.get_response()
        await conv.send_message("/link")
        reply = await conv.get_response()
    match = URL_RE.search(response_text(reply))
    if not match:
        raise RecoveryError("IncogNoteBot did not return the expected unique link.")
    return match.group(0)


async def revoke_anonymous_link(client: TelegramClient, anonymous_bot: str) -> None:
    async with client.conversation(anonymous_bot, timeout=45, exclusive=True) as conv:
        await conv.send_message("/change")
        await conv.get_response()
        await conv.send_message("Yes, I am sure")
        reply = await conv.get_response()
    if any(word in response_text(reply).casefold() for word in ("error", "failed", "invalid")):
        raise RecoveryError("IncogNoteBot did not confirm link revocation: " + response_text(reply))


def button_labels(message: object) -> list[str]:
    return [str(getattr(button, "text", "") or "") for row in (getattr(message, "buttons", None) or ()) for button in row]


async def click_button(message: object, needle: str) -> bool:
    for row, buttons in enumerate(getattr(message, "buttons", None) or ()):
        for column, button in enumerate(buttons):
            if needle.casefold() in str(getattr(button, "text", "") or "").casefold():
                await message.click(row, column)
                return True
    return False


async def transfer_ownership(client: TelegramClient, bot_handle: str, recipient: str) -> None:
    """Drive BotFather's documented /mybots ownership-transfer interaction."""
    async with client.conversation(BOTFATHER, timeout=60, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/mybots")
        listing = await conv.get_response()
        if not await click_button(listing, bot_handle):
            raise RecoveryError(f"BotFather did not list {bot_handle} as owned by the pool account.")
        menu = await conv.get_response()
        if not await click_button(menu, "transfer ownership"):
            raise RecoveryError("BotFather has no Transfer Ownership control. Buttons: " + ", ".join(button_labels(menu)))
        await conv.get_response()
        await conv.send_message(recipient)
        confirmation = await conv.get_response()
        if not await click_button(confirmation, "yes, i am sure"):
            await conv.send_message("Yes, I am sure")
        final = await conv.get_response()
    text = response_text(final).casefold()
    if any(word in text for word in ("error", "failed", "invalid", "sorry")):
        raise RecoveryError("BotFather rejected ownership transfer: " + response_text(final))


def replace_usernames(value: str, mappings: list[tuple[str, str]]) -> str:
    result = value
    for old, new in mappings:
        result = re.sub(re.escape(old), new, result, flags=re.IGNORECASE)
    return result


async def get_source_commands(client: TelegramClient, source: str, api_id: int, api_hash: str) -> list[types.BotCommand]:
    async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/token")
        await conv.get_response()
        await conv.send_message(source)
        token_message = await conv.get_response()
    match = TOKEN_RE.search(response_text(token_message))
    if not match:
        raise RecoveryError(f"Could not read the source token for {source}.")
    bot_client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await bot_client.start(bot_token=match.group(0))
        return list(await bot_client(functions.bots.GetBotCommandsRequest(scope=types.BotCommandScopeDefault(), lang_code="")))
    finally:
        await bot_client.disconnect()


async def set_bot_info(client: TelegramClient, target: str, name: str, about: str, description: str) -> None:
    for command, value in (("/setname", name), ("/setabouttext", about), ("/setdescription", description)):
        async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
            await conv.send_message("/cancel")
            await conv.get_response()
            await conv.send_message(command)
            await conv.get_response()
            await conv.send_message(target)
            await conv.get_response()
            await conv.send_message(value.replace("\r\n", "\n") or "/empty")
            reply = await conv.get_response()
        if "error" in response_text(reply).casefold():
            raise RecoveryError(f"BotFather rejected {command}: {response_text(reply)}")


async def set_commands(client: TelegramClient, target: str, commands: list[types.BotCommand]) -> None:
    payload = "\n".join(f"{item.command} - {item.description}" for item in commands) or "/empty"
    async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/setcommands")
        await conv.get_response()
        await conv.send_message(target)
        await conv.get_response()
        await conv.send_message(payload)
        reply = await conv.get_response()
    if "error" in response_text(reply).casefold():
        raise RecoveryError("BotFather rejected command update: " + response_text(reply))


async def regenerate_token(client: TelegramClient, target: str) -> str:
    async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/revoke")
        await conv.get_response()
        await conv.send_message(target)
        response = await conv.get_response()
    match = TOKEN_RE.search(response_text(response))
    if not match:
        raise RecoveryError("BotFather did not return a replacement token for " + target)
    return match.group(0)


def snapshot_metadata_path(config: Config, mapping: BotMapping) -> Path:
    return config.snapshot_dir / mapping.handle_prefix.casefold() / "snapshot.json"


def load_snapshot(config: Config, mapping: BotMapping) -> dict[str, Any]:
    path = snapshot_metadata_path(config, mapping)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"Could not read bot snapshot {path}: {exc}") from exc
    required = ("source_handle", "name", "about", "description", "commands")
    if not isinstance(snapshot, dict) or any(key not in snapshot for key in required):
        raise RecoveryError(f"Bot snapshot {path} is incomplete.")
    if not all(isinstance(snapshot[key], str) for key in required[:-1]):
        raise RecoveryError(f"Bot snapshot {path} contains invalid profile text.")
    if not isinstance(snapshot["commands"], list):
        raise RecoveryError(f"Bot snapshot {path} has an invalid commands list.")
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and isinstance(item.get("description"), str)
        for item in snapshot["commands"]
    ):
        raise RecoveryError(f"Bot snapshot {path} contains an invalid command.")
    photo_name = snapshot.get("profile_photo")
    if photo_name is not None:
        if not isinstance(photo_name, str) or Path(photo_name).name != photo_name:
            raise RecoveryError(f"Bot snapshot {path} has an invalid profile photo path.")
        if not (path.parent / photo_name).is_file():
            raise RecoveryError(f"Snapshot profile photo is missing: {path.parent / photo_name}")
    return snapshot


async def snapshot_bot(
    client: TelegramClient,
    api_id: int,
    api_hash: str,
    config: Config,
    mapping: BotMapping,
) -> None:
    assert mapping.handle
    source = await client.get_input_entity(mapping.handle)
    source_entity = await client.get_entity(source)
    if not getattr(source_entity, "bot", False):
        raise RecoveryError(f"Snapshot source {mapping.handle} is not a bot.")
    info = await client(functions.bots.GetBotInfoRequest(lang_code="", bot=source))
    commands = await get_source_commands(client, mapping.handle, api_id, api_hash)
    directory = snapshot_metadata_path(config, mapping).parent
    directory.mkdir(parents=True, exist_ok=True)
    photo = await client.download_profile_photo(
        source_entity,
        file=str(directory / "profile-photo"),
        download_big=True,
    )
    snapshot = {
        "source_handle": mapping.handle,
        "name": source_entity.first_name or "",
        "about": info.about or "",
        "description": info.description or "",
        "commands": [
            {"command": item.command, "description": item.description}
            for item in commands
        ],
        "profile_photo": Path(photo).name if photo else None,
    }
    path = snapshot_metadata_path(config, mapping)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    try:
        os.chmod(directory, 0o700)
        os.chmod(path, 0o600)
        if photo:
            os.chmod(photo, 0o600)
    except OSError:
        pass


async def restore_bot_snapshot(
    client: TelegramClient,
    config: Config,
    mapping: BotMapping,
    old_handle: str,
    target_name: str,
) -> str:
    snapshot = load_snapshot(config, mapping)
    source_handle = normalize_username(str(snapshot["source_handle"]))
    replacements = [(source_handle, target_name)]
    if old_handle.casefold() != source_handle.casefold():
        replacements.append((old_handle, target_name))
    await set_bot_info(
        client,
        target_name,
        replace_usernames(str(snapshot["name"]), replacements),
        replace_usernames(str(snapshot["about"]), replacements),
        replace_usernames(str(snapshot["description"]), replacements),
    )
    photo_name = snapshot.get("profile_photo")
    if photo_name:
        photo = snapshot_metadata_path(config, mapping).parent / str(photo_name)
        if not photo.is_file():
            raise RecoveryError(f"Snapshot profile photo is missing: {photo}")
        target = await client.get_input_entity(target_name)
        uploaded = await client.upload_file(str(photo))
        await client(functions.photos.UploadProfilePhotoRequest(bot=target, file=uploaded))
    try:
        commands = [
            types.BotCommand(
                command=str(item["command"]),
                description=replace_usernames(str(item["description"]), replacements),
            )
            for item in snapshot["commands"]
        ]
    except (KeyError, TypeError) as exc:
        raise RecoveryError("The saved bot command snapshot is invalid.") from exc
    await set_commands(client, target_name, commands)
    return await regenerate_token(client, target_name)


def write_yaml_value(path: Path, dotted_key: str, value: str) -> None:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RecoveryError(f"Could not read deployment config {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise RecoveryError(f"Deployment config {path} must be a YAML mapping.")
    node = document
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise RecoveryError(f"{dotted_key} cannot be written: {part} is not a mapping.")
        node = child
    node[parts[-1]] = value
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    temporary.replace(path)


def restart(command: tuple[str, ...]) -> None:
    try:
        subprocess.run(command, check=True, timeout=60, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError(f"Restart command failed ({' '.join(command)}): {exc}") from exc


async def bot_needs_recovery(client: TelegramClient, handle: str) -> bool:
    try:
        entity = await client.get_entity(handle)
        if not getattr(entity, "bot", False) or getattr(entity, "deleted", False):
            return True
        peer = await client.get_input_entity(entity)
        peer_settings = await client(functions.messages.GetPeerSettingsRequest(peer))
        settings = getattr(peer_settings, "settings", peer_settings)
        if getattr(settings, "blocked", False):
            return True
        async with client.conversation(handle, timeout=45, exclusive=True) as conv:
            sent = await conv.send_message("/about")
            await conv.get_reply(sent)
        return False
    except (asyncio.TimeoutError, OSError, RPCError, ValueError):
        return True


async def update_runtime_about(
    client: TelegramClient,
    new_handle: str,
    old_handle: str,
) -> None:
    async with client.conversation(new_handle, timeout=90, exclusive=True) as conv:
        sent = await conv.send_message("/about")
        current = await conv.get_reply(sent)
        updated, replacements = re.subn(
            re.escape(old_handle),
            new_handle,
            response_text(current),
            flags=re.IGNORECASE,
        )
        if replacements:
            sent = await conv.send_message("/about " + updated)
            about_result = await conv.get_reply(sent)
            about_text = response_text(about_result).casefold()
            if any(word in about_text for word in ("error", "invalid", "admin only", "failed")):
                raise RecoveryError(
                    f"{new_handle} rejected its /about update: "
                    + response_text(about_result)
                )
            sent = await conv.send_message("/reload")
            reload_result = await conv.get_reply(sent)
            reload_text = response_text(reload_result).casefold()
            if any(word in reload_text for word in ("error", "invalid", "admin only", "failed")):
                raise RecoveryError(
                    f"{new_handle} rejected /reload: "
                    + response_text(reload_result)
                )


def update_configured_handle(
    config: Config, mapping: BotMapping, new_handle: str
) -> None:
    bots = required_list(config.value, "bots")
    entry = next(
        (
            item for item in bots
            if isinstance(item, dict)
            and str(item.get("handle_prefix", "")).lstrip("@").casefold()
            == mapping.handle_prefix.casefold()
        ),
        None,
    )
    if entry is None:
        raise RecoveryError(
            f"Could not update the configured handle for {mapping.friendly_name}."
        )
    entry["handle"] = new_handle
    temporary = config.source.with_suffix(config.source.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(config.value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(config.source)


def update_index(html: str, mapping: BotMapping, new_handle: str) -> str:
    label = f"Updated: {date.today().isoformat()} (restored bot {mapping.friendly_name})"
    updated, _ = re.subn(r"Updated:\s*[^<\r\n]+", label, html, count=1, flags=re.IGNORECASE)
    pattern = re.compile(rf"https://t\.me/(?P<at>@?){re.escape(mapping.handle_prefix)}[A-Za-z0-9_]*", re.IGNORECASE)
    updated, _ = pattern.subn(lambda match: f"https://t.me/{match.group('at')}{new_handle.lstrip('@')}", updated)
    return updated


async def publish_page(config: Config, mapping: BotMapping, new_handle: str) -> None:
    cf = config.cloudflare
    project = required_string(cf, "project_name")
    pages_url = required_string(cf, "pages_url").rstrip("/") + "/"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.get(pages_url) as response:
            if response.status >= 400:
                raise RecoveryError(f"Could not fetch Pages index ({response.status}).")
            original = await response.text()
        content = update_index(original, mapping, new_handle)
    with tempfile.TemporaryDirectory(prefix="forward-bot-pages-") as output_dir:
        Path(output_dir, "index.html").write_text(content, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            "npx", "wrangler", "pages", "deploy", output_dir,
            "--project", project,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = (stderr or stdout).decode(errors="replace").strip()
            raise RecoveryError(f"Wrangler Pages deploy failed: {detail}")


def load_config(path: Path) -> Config:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("The recovery configuration must be a YAML mapping.")
    return Config(value, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SETTINGS_PATH)
    parser.add_argument("--check-config", action="store_true", help="validate config then exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"Configuration is valid: {args.config}")
            return 0
        asyncio.run(Service(config).run())
        return 0
    except (RecoveryError, RPCError, ValueError, OSError) as exc:
        logging.getLogger("recovery").error("Recovery service stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
