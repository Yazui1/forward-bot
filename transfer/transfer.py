#!/usr/bin/env python3
"""Stateful recovery service for the Forward Bot fleet."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

import aiohttp
import yaml
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import (
    ChatAboutNotModifiedError,
    FloodWaitError,
    MessageIdInvalidError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yml"
BOTFATHER = "BotFather"
ABUSE_SENDER = "AbuseNotification"
ANONYMOUS_BOT = "@IncogNoteBot"
BOTFATHER_WEBAPP = "https://webappinternal.telegram.org/botfather"
CREATE_INTERVAL = 300
AUTOMATIC_WAIT_LIMIT = 300
BOTFATHER_CHAT_COOLDOWN = 24 * 60 * 60
BOTFATHER_MINI_APP_COOLDOWN = 6 * 60 * 60
BOTFATHER_COOLDOWN_STATE_VERSION = 2
BOTFATHER_ACTION_DELAY = 5
MONITOR_INTERVAL = 300
RETRY_MONITOR_INTERVAL = 30
TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
HANDLE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,})\b")
ANON_LINK_RE = re.compile(r"https://t\.me/IncogNoteBot\?start=[A-Za-z0-9_-]+")
REWARD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
OWNERSHIP_PHRASE = "ownership of the bot"
LOG = logging.getLogger("forward_bot_recovery")


class RecoveryError(RuntimeError):
    """An expected operational failure that can be logged with context."""


class BotFatherRateLimited(RecoveryError):
    def __init__(self, message: str, retry_at: float | None = None, *,
                 method: str = "chat", operation: str | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at
        self.method = method
        self.operation = operation


class MiniAppAuthExpired(RecoveryError):
    pass


class MiniAppBadRequest(RecoveryError):
    pass


class MiniAppRateLimited(RecoveryError):
    def __init__(self, message: str, *, operation: str | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class AutomaticWaitExceeded(RecoveryError):
    pass


@dataclass(frozen=True)
class Account:
    username: str
    session: Path


@dataclass(frozen=True)
class BotSpec:
    prefix: str
    friendly_name: str
    config_path: Path
    token_path: str
    restart_command: tuple[str, ...]
    handle: str | None = None


@dataclass
class Recovery:
    key: str
    old_handle: str
    receiver: str
    anonymous_link: str = ""
    stage: str = "starting"
    new_handle: str | None = None
    anonymous_message_id: int | None = None
    announcement_message_id: int | None = None
    candidate_handles: list[str] = field(default_factory=list)
    candidate_message_ids: dict[str, int] = field(default_factory=dict)
    reward_code: str = ""
    botfather_chat_mode: bool = False
    botfather_retry_at: float = 0.0
    mode: str = "manual"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Recovery:
        # Accept the old state spelling so an in-place rollout can resume work.
        if "receiver" not in value:
            value = dict(value)
            value["receiver"] = value.pop(
                "receiver_username", value.pop("pool_username", ""))
        if "botfather_retry_at" not in value:
            value["botfather_retry_at"] = value.pop(
                "botfather_retry_after", 0.0)
        if "mode" not in value:
            stage = str(value.get("stage", ""))
            automatic_finalization = (
                stage == "finalizing"
                and not value.get("anonymous_link")
                and value.get("anonymous_message_id") is None
            )
            value["mode"] = (
                "automatic" if stage.startswith("automatic_") or automatic_finalization
                else "manual")
        return cls(**value)


@dataclass
class BackupDonation:
    """A user-submitted bot being transferred into the main account's spare pool."""

    handle: str
    key: str
    receiver: str
    anonymous_message_id: int
    stage: str = "waiting_receiver"
    botfather_chat_mode: bool = False
    botfather_retry_at: float = 0.0


@dataclass
class RuntimeState:
    recoveries: dict[str, Recovery] = field(default_factory=dict)
    backups: dict[str, list[str]] = field(default_factory=dict)
    backup_donations: dict[str, BackupDonation] = field(default_factory=dict)
    anonymous_links: dict[str, str] = field(default_factory=dict)
    pending_creations: dict[str, str] = field(default_factory=dict)
    pending_creation_chat_modes: dict[str, bool] = field(default_factory=dict)
    creator_next_at: float = 0.0
    creator_retry_at: float = 0.0
    botfather_cooldowns: dict[str, float] = field(default_factory=dict)
    botfather_cooldown_version: int = 0


class Config:
    """Validated configuration plus the YAML document used for updates."""

    def __init__(self, raw: dict[str, Any], path: Path) -> None:
        self.raw = raw
        self.path = path.expanduser().resolve()
        base = self.path.parent

        telegram = mapping(raw, "telegram")
        self.api_id = integer_string(telegram, "api_id")
        self.api_hash = string(telegram, "api_hash")
        self.main = account(mapping(telegram, "main"), base)
        self.pool = tuple(account(item, base) for item in list_value(
            telegram, "pool", optional=True))

        automatic = raw.get("automatic_recovery", {})
        if not isinstance(automatic, dict):
            raise ValueError("automatic_recovery must be a YAML mapping.")
        self.automatic_enabled = boolean(automatic, "enabled", False)
        self.prepare_bots = boolean(automatic, "prepare_bots", False)
        if self.prepare_bots and not self.automatic_enabled:
            raise ValueError("automatic_recovery.prepare_bots requires enabled: true.")
        creator_data = automatic.get("creator")
        self.creator: Account | None = None
        self.creator_api_id: int | None = None
        self.creator_api_hash: str | None = None
        self.creator_password: str | None = None
        if self.prepare_bots and not isinstance(creator_data, dict):
            raise ValueError(
                "automatic_recovery.creator is required when prepare_bots is enabled.")
        if creator_data is not None:
            if not isinstance(creator_data, dict):
                raise ValueError("automatic_recovery.creator must be a YAML mapping.")
            self.creator = account(creator_data, base)
            self.creator_api_id = integer_string(creator_data, "api_id")
            self.creator_api_hash = string(creator_data, "api_hash")
            password = creator_data.get("password")
            if password is not None and not isinstance(password, str):
                raise ValueError("automatic_recovery.creator.password must be a string.")
            self.creator_password = password or None

        announcement = mapping(raw, "announcement")
        self.announcement_channel = string(announcement, "channel")
        self.announcement_group = string(announcement, "group")
        self.recovery_template = string(announcement, "recovery_template")
        self.restored_template = string(announcement, "restored_template")

        anonymous_submissions = raw.get("anonymous_submissions", {})
        if not isinstance(anonymous_submissions, dict):
            raise ValueError("anonymous_submissions must be a YAML mapping.")
        self.retain_anonymous_link = boolean(
            anonymous_submissions, "retain_link_after_recovery", False)
        donation_template = anonymous_submissions.get(
            "donation_template",
            "Contribute a spare {friendly_name} bot by sending its @username here: {anonymous_link}")
        if not isinstance(donation_template, str):
            raise ValueError("anonymous_submissions.donation_template must be a string.")
        if self.retain_anonymous_link and not donation_template.strip():
            raise ValueError(
                "anonymous_submissions.donation_template cannot be empty when link retention is enabled.")
        self.donation_template = donation_template

        cloudflare = mapping(raw, "cloudflare")
        self.pages_project = string(cloudflare, "project_name")
        self.pages_url = string(cloudflare, "pages_url").rstrip("/") + "/"

        self.state_path = resolve_path(
            raw.get("state_path", ".state/recovery.json"), base)
        self.snapshot_dir = resolve_path(
            raw.get("snapshot_dir", ".state/snapshots"), base)
        self.bots = tuple(self._parse_bots(list_value(raw, "bots"), base))

        prefixes = [bot.prefix.casefold() for bot in self.bots]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("Every bots.handle_prefix must be unique.")
        for index, prefix in enumerate(prefixes):
            if any(other.startswith(prefix) or prefix.startswith(other)
                   for other in prefixes[index + 1:]):
                raise ValueError(
                    "Bot handle prefixes must not overlap; recovery matching must be unambiguous.")
        accounts = [self.main, *self.pool]
        if len({item.username.casefold() for item in accounts}) != len(accounts):
            raise ValueError("Main and pool account usernames must be unique.")
        if self.creator and self.creator.username.casefold() in {
                item.username.casefold() for item in accounts}:
            raise ValueError("The creator account must differ from main and pool accounts.")

    @staticmethod
    def _parse_bots(items: list[Any], base: Path) -> list[BotSpec]:
        parsed: list[BotSpec] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Every bots entry must be a YAML mapping.")
            prefix = string(item, "handle_prefix").lstrip("@")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", prefix) is None:
                raise ValueError(
                    "bots.handle_prefix must use Telegram username characters and start with a letter.")
            if len(prefix) + 5 > 32:
                raise ValueError(
                    f"bots.handle_prefix {prefix!r} leaves no valid replacement username length.")
            command = item.get("restart_command")
            if isinstance(command, str):
                command = shlex.split(command)
            if (not isinstance(command, list) or not command
                    or any(not isinstance(part, str) or not part for part in command)):
                raise ValueError(
                    "bots.restart_command must be a non-empty string or argument list.")
            handle_value = item.get("handle")
            handle = normalize_handle(str(handle_value)) if handle_value else None
            if handle and not valid_bot_username(handle):
                raise ValueError(f"bots.handle {handle} is not a valid bot username.")
            token_path = string(item, "token_path")
            if not all(token_path.split(".")):
                raise ValueError("bots.token_path must be a dotted YAML key.")
            parsed.append(BotSpec(
                prefix=prefix,
                friendly_name=string(item, "friendly_name"),
                config_path=resolve_path(string(item, "config_path"), base),
                token_path=token_path,
                restart_command=tuple(command),
                handle=handle,
            ))
        if not parsed:
            raise ValueError("bots must contain at least one configured bot.")
        return parsed

    def mapping_for_handle(self, handle: str) -> BotSpec | None:
        clean = handle_key(handle)
        matches = [bot for bot in self.bots if clean.startswith(bot.prefix.casefold())]
        if len(matches) > 1:
            raise RecoveryError(f"More than one mapping matches @{clean}.")
        return matches[0] if matches else None

    def mapping_for_key(self, key: str) -> BotSpec:
        for bot in self.bots:
            if bot.prefix.casefold() == key.casefold():
                return bot
        raise RecoveryError(f"Configured mapping {key!r} no longer exists.")

    def set_current_handle(self, bot: BotSpec, handle: str) -> None:
        entries = self.raw.get("bots")
        if not isinstance(entries, list):
            raise RecoveryError("Configuration bots value is no longer a list.")
        entry = next((value for value in entries
                      if isinstance(value, dict)
                      and str(value.get("handle_prefix", "")).lstrip("@").casefold()
                      == bot.prefix.casefold()), None)
        if entry is None:
            raise RecoveryError(f"Could not find {bot.friendly_name} in config.")
        entry["handle"] = handle
        atomic_write_text(self.path, yaml.safe_dump(
            self.raw, sort_keys=False, allow_unicode=True))
        self.bots = tuple(
            replace(item, handle=handle) if item.prefix.casefold() == bot.prefix.casefold()
            else item for item in self.bots
        )


def mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ValueError(f"{key} must be a YAML mapping.")
    return result


def list_value(value: dict[str, Any], key: str, *, optional: bool = False) -> list[Any]:
    result = value.get(key)
    if optional and result is None:
        return []
    if not isinstance(result, list):
        raise ValueError(f"{key} must be a YAML list.")
    return result


def string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return result.strip()


def integer_string(value: dict[str, Any], key: str) -> int:
    raw = string(value, key)
    try:
        result = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer.") from exc
    if result <= 0:
        raise ValueError(f"{key} must be positive.")
    return result


def boolean(value: dict[str, Any], key: str, default: bool) -> bool:
    result = value.get(key, default)
    if not isinstance(result, bool):
        raise ValueError(f"{key} must be true or false.")
    return result


def resolve_path(value: object, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base / path


import traceback

def normalize_handle(value: str) -> str:
    clean = value.strip().lstrip("@")
    if not clean:
       raise ValueError("A Telegram username is required.")
    return "@" + clean


def handle_key(value: str) -> str:
    return value.lstrip("@").casefold()


def valid_bot_username(value: str) -> bool:
    clean = value.lstrip("@")
    return bool(len(clean) <= 32
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,}", clean)
                and clean.casefold().endswith("bot"))


def account(value: dict[str, Any], base: Path) -> Account:
    return Account(normalize_handle(string(value, "username")),
                   resolve_path(string(value, "session"), base))


def response_text(message: object) -> str:
    return str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")


def message_handles(message: object) -> set[str]:
    return {"@" + match.group(1) for match in HANDLE_RE.finditer(response_text(message))}


def button_labels(message: object) -> list[str]:
    return [str(getattr(button, "text", "") or "")
            for row in (getattr(message, "buttons", None) or ())
            for button in row]


def render(template: str, **values: str) -> str:
    try:
        return template.format(**values)
    except (KeyError, ValueError) as exc:
        raise RecoveryError(f"Invalid announcement template: {exc}") from exc


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def bot_handle_replacer(prefix: str, new_handle: str, text: str) -> str:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_])(?P<at>@?){re.escape(prefix)}\d+bot(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )
    replacement = new_handle.lstrip("@")
    return pattern.sub(lambda match: match.group("at") + replacement, text)


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RecoveryError(f"Could not read YAML file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"YAML file {path} must contain a mapping.")
    return value


def write_yaml_path(path: Path, dotted_key: str, value: str) -> None:
    document = read_yaml(path)
    node = document
    parts = dotted_key.split(".")
    if not all(parts):
        raise RecoveryError(f"Invalid dotted YAML key: {dotted_key!r}.")
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise RecoveryError(f"Cannot write {dotted_key}: {part} is not a mapping.")
        node = child
    node[parts[-1]] = value
    atomic_write_text(path, yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True))


def reward_code() -> str:
    return "".join(secrets.choice(REWARD_CHARS) for _ in range(4))


class StateStore:
    """Small atomic JSON store. All mutations are coordinated by the service lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = RuntimeState()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state root must be an object")
            loaded: dict[str, Recovery] = {}
            generated_reward = False
            saved_modes = payload.get("recovery_modes", {})
            if not isinstance(saved_modes, dict):
                saved_modes = {}
            for raw in payload.get("recoveries", []):
                if not isinstance(raw, dict):
                    raise ValueError("each recovery must be an object")
                raw = dict(raw)
                if "mode" not in raw and raw.get("key") in saved_modes:
                    raw["mode"] = saved_modes[raw["key"]]
                recovery = Recovery.from_dict(raw)
                recovery.receiver = normalize_handle(recovery.receiver).casefold()
                if recovery.new_handle and all(
                        handle_key(item) != handle_key(recovery.new_handle)
                        for item in recovery.candidate_handles):
                    recovery.candidate_handles.append(recovery.new_handle)
                if recovery.new_handle and recovery.anonymous_message_id is not None:
                    recovery.candidate_message_ids.setdefault(
                        handle_key(recovery.new_handle), recovery.anonymous_message_id)
                if not recovery.reward_code and recovery.mode == "manual":
                    # Repair old manual records which predate reward codes.
                    recovery.reward_code = reward_code()
                    generated_reward = True
                loaded[recovery.key.casefold()] = recovery
            backups_raw = payload.get("backups", {})
            donations_raw = payload.get("backup_donations", [])
            links_raw = payload.get("anonymous_links", {})
            pending_raw = payload.get("pending_creations", {})
            pending_modes_raw = payload.get("pending_creation_chat_modes", {})
            cooldowns_raw = payload.get("botfather_cooldowns", {})
            if (not isinstance(backups_raw, dict) or not isinstance(donations_raw, list)
                    or not isinstance(links_raw, dict) or not isinstance(pending_raw, dict)
                    or not isinstance(pending_modes_raw, dict)
                    or not isinstance(cooldowns_raw, dict)):
                raise ValueError(
                    "backups, anonymous_links, pending creations, and pending modes must be objects; "
                    "backup_donations must be a list and botfather_cooldowns must be an object")
            backups = {}
            for key, values in backups_raw.items():
                if not isinstance(values, list):
                    raise ValueError(f"backups.{key} must be a list")
                backups[str(key).casefold()] = [normalize_handle(str(v)) for v in values]
            donations = {}
            for raw_donation in donations_raw:
                if not isinstance(raw_donation, dict):
                    raise ValueError("each backup donation must be an object")
                donation = BackupDonation(**raw_donation)
                donation.handle = normalize_handle(donation.handle)
                donation.key = str(donation.key).casefold()
                donation.receiver = normalize_handle(donation.receiver).casefold()
                if donation.stage not in {"waiting_receiver", "transferring_main"}:
                    raise ValueError(f"Unknown backup donation stage {donation.stage!r}")
                donations[handle_key(donation.handle)] = donation
            anonymous_links = {}
            for receiver, link in links_raw.items():
                if not isinstance(link, str):
                    raise ValueError(f"anonymous_links.{receiver} must be a string")
                anonymous_links[str(receiver).casefold()] = link
            # Carry active links forward from state files written before links
            # became persistent across completed recoveries.
            for recovery in loaded.values():
                if recovery.anonymous_link:
                    anonymous_links.setdefault(
                        recovery.receiver.casefold(), recovery.anonymous_link)
            pending = {str(key).casefold(): normalize_handle(str(value))
                       for key, value in pending_raw.items()}
            pending_modes = {str(key).casefold(): value is True
                             for key, value in pending_modes_raw.items()}
            cooldowns = {str(key): float(value or 0)
                         for key, value in cooldowns_raw.items()}
            self.state = RuntimeState(
                recoveries=loaded,
                backups=backups,
                backup_donations=donations,
                anonymous_links=anonymous_links,
                pending_creations=pending,
                pending_creation_chat_modes=pending_modes,
                creator_next_at=float(payload.get("creator_next_create_at", 0) or 0),
                creator_retry_at=float(payload.get("creator_retry_after", 0) or 0),
                botfather_cooldowns=cooldowns,
                botfather_cooldown_version=int(
                    payload.get("botfather_cooldown_version", 0) or 0),
            )
            if generated_reward:
                self.save()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RecoveryError(f"Could not load recovery state: {exc}") from exc

    def save(self) -> None:
        state = self.state
        recoveries = []
        modes = {}
        for recovery in state.recoveries.values():
            item = asdict(recovery)
            modes[recovery.key] = item.pop("mode")
            item["receiver_username"] = item.pop("receiver")
            item["botfather_retry_after"] = item.pop("botfather_retry_at")
            recoveries.append(item)
        payload = {
            "recoveries": recoveries,
            "recovery_modes": modes,
            "backups": state.backups,
            "backup_donations": [asdict(item) for item in state.backup_donations.values()],
            "anonymous_links": state.anonymous_links,
            "pending_creations": state.pending_creations,
            "pending_creation_chat_modes": state.pending_creation_chat_modes,
            "creator_next_create_at": state.creator_next_at,
            "creator_retry_after": state.creator_retry_at,
            "botfather_cooldowns": state.botfather_cooldowns,
            "botfather_cooldown_version": state.botfather_cooldown_version,
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")


class BotFatherMiniApp:
    """Authenticated HTTP adapter for BotFather's internal Mini App API."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.user_id: int | None = None
        self.identity_checked = False
        session_name = getattr(getattr(client, "session", None), "filename", None)
        if session_name and str(session_name) != ":memory:":
            stem = Path(str(session_name))
            self.cookie_path = Path(str(stem) + ".botfather.cookies.txt")
            self.cache_path = Path(str(stem) + ".botfather.json")
            self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
            self.cookies = MozillaCookieJar(str(self.cookie_path))
            if self.cookie_path.exists():
                try:
                    self.cookies.load(ignore_discard=True, ignore_expires=True)
                except Exception:
                    self.cookies.clear()
            self.api_hash: str | None = None
            try:
                cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(cache, dict):
                    cached_hash = cache.get("api_hash")
                    if isinstance(cached_hash, str) and re.fullmatch(
                            r"[A-Za-z0-9_-]+", cached_hash):
                        self.api_hash = cached_hash
                    cached_id = cache.get("user_id")
                    if isinstance(cached_id, int) and not isinstance(cached_id, bool) and cached_id > 0:
                        self.user_id = cached_id
            except (OSError, ValueError):
                pass
        else:
            self.cookie_path = None
            self.cache_path = None
            self.cookies = MozillaCookieJar()
            self.api_hash = None
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def _save(self) -> None:
        if self.cookie_path is None or self.cache_path is None:
            return
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_tmp = self.cookie_path.with_name(
            self.cookie_path.name + f".{os.getpid()}.tmp")
        try:
            self.cookies.save(str(cookie_tmp), ignore_discard=True, ignore_expires=True)
            os.chmod(cookie_tmp, 0o600)
            os.replace(cookie_tmp, self.cookie_path)
        except OSError:
            LOG.exception("Could not persist BotFather Mini App cookies")
            try:
                cookie_tmp.unlink()
            except OSError:
                pass
        if self.api_hash:
            atomic_write_text(self.cache_path, json.dumps({
                "api_hash": self.api_hash,
                **({"user_id": self.user_id} if self.user_id else {}),
            }) + "\n")
        else:
            try:
                self.cache_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOG.exception("Could not clear BotFather Mini App cache")

    def _request_sync(self, url: str, form: dict[str, str] | None = None
                      ) -> tuple[int, str, str]:
        operation = str(form.get("method") or "webapp_page") if form else "webapp_page"
        method = "POST" if form is not None else "GET"
        path = urlparse(url).path or "/"
        started = time.monotonic()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
        }
        body = None
        if form is not None:
            body = urlencode(form).encode("utf-8")
            referer = BOTFATHER_WEBAPP
            if form.get("method") == "createBot":
                referer = f"{BOTFATHER_WEBAPP}/create"
            elif form.get("method") == "requestTransferBot":
                bid = str(form.get("bid") or "")
                if re.fullmatch(r"\d+", bid):
                    referer = f"{BOTFATHER_WEBAPP}/bot/{bid}/transfer"
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://webappinternal.telegram.org",
                "Priority": "u=1, i",
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
            })
        request = Request(url, data=body, headers=headers)
        try:
            try:
                with self.opener.open(request, timeout=30) as response:
                    status = response.status
                    response_body = response.read().decode("utf-8", "replace")
                    final_url = response.geturl()
            except HTTPError as response:
                try:
                    status = response.code
                    response_body = response.read().decode("utf-8", "replace")
                    final_url = response.geturl()
                finally:
                    response.close()
            elapsed_ms = (time.monotonic() - started) * 1000
            summary = self._response_summary(response_body)
            if status >= 400:
                LOG.warning("Mini App %s %s %s -> HTTP %s in %.0f ms%s",
                            operation, method, path, status, elapsed_ms,
                            f"; {summary}" if summary else "")
            else:
                LOG.info("Mini App %s %s %s -> HTTP %s in %.0f ms%s",
                         operation, method, path, status, elapsed_ms,
                         f"; {summary}" if summary else "")
            return status, response_body, final_url
        except URLError as exc:
            # URLError strings can include the API URL and its session hash.
            reason = getattr(exc, "reason", None)
            reason_type = type(reason).__name__ if reason is not None else "unknown"
            LOG.error("Mini App %s %s %s failed to connect (%s)",
                      operation, method, path, reason_type)
            raise RecoveryError("Could not reach the BotFather Mini App.") from None
        except Exception as exc:
            # Request exceptions may include a URL, response data, or form details.
            LOG.error("Mini App %s %s %s failed (%s)",
                      operation, method, path, type(exc).__name__)
            raise RecoveryError("BotFather Mini App request failed unexpectedly.") from None
        finally:
            # Never log raw request bodies, initData, cookies, or response bodies.
            self._save()

    @staticmethod
    def _response_summary(body: str) -> str:
        try:
            payload = json.loads(body)
        except ValueError:
            return f"non-JSON response ({len(body)} chars)" if body else "empty response"
        if not isinstance(payload, dict):
            return f"JSON {type(payload).__name__} response"
        parts = []
        for key in ("ok", "error", "msg", "bot_id"):
            value = payload.get(key)
            if value is None:
                continue
            rendered = str(value)
            rendered = TOKEN_RE.sub("[redacted bot token]", rendered)
            rendered = re.sub(r"(?i)(hash=)[A-Za-z0-9_-]+", r"\1[redacted]", rendered)
            rendered = re.sub(
                r"(?i)(stel_(?:bot_father_token|ssid)=)[^;\s&]+",
                r"\1[redacted]", rendered)
            rendered = re.sub(r"[\r\n\t]+", " ", rendered)[:160]
            parts.append(f"{key}={rendered}")
        return ", ".join(parts) if parts else "JSON object response"

    async def _request(self, url: str, form: dict[str, str] | None = None
                       ) -> tuple[int, str, str]:
        return await asyncio.to_thread(self._request_sync, url, form)

    @staticmethod
    def _api_error(body: str) -> str | None:
        try:
            payload = json.loads(body)
        except ValueError:
            return "Error" if body.strip().casefold() == "error" else None
        if isinstance(payload, dict) and payload.get("error") is not None:
            return str(payload["error"])
        return None

    @staticmethod
    def _access_denied(status: int, body: str, _final_url: str) -> bool:
        api_error = BotFatherMiniApp._api_error(body)
        if api_error and api_error.strip().casefold() == "error":
            return False
        # A redirect alone is not enough to call this an expired session:
        # BotFather can redirect a rate-limited request as well.
        if status in (401, 403):
            return True
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                details = [payload.get("error"), payload.get("msg")]
                body = " ".join(str(item) for item in details if item is not None)
        except ValueError:
            pass
        normalized = re.sub(r"[_-]+", " ", body.casefold())
        return any(phrase in normalized for phrase in (
            "auth expired", "authentication expired", "session expired",
            "session has expired", "expired session", "token expired",
            "token has expired", "invalid token", "invalid hash", "hash expired",
        ))

    @staticmethod
    def _decode_json(status: int, body: str, method: str) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except ValueError as exc:
            if status == 400:
                LOG.warning("Mini App %s returned HTTP 400 without a usable JSON body",
                            method)
                raise MiniAppBadRequest(
                    f"BotFather Mini App {method} returned HTTP 400.") from exc
            if not 200 <= status < 300:
                LOG.error("Mini App %s returned an unsuccessful HTTP status (%s)",
                          method, status)
                raise RecoveryError(
                    f"BotFather Mini App {method} returned HTTP {status}.") from exc
            LOG.error("Mini App %s returned invalid JSON", method)
            raise RecoveryError(f"BotFather Mini App {method} returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            if status == 400:
                LOG.warning("Mini App %s returned HTTP 400 with an unexpected JSON shape",
                            method)
                raise MiniAppBadRequest(
                    f"BotFather Mini App {method} returned HTTP 400.")
            if not 200 <= status < 300:
                LOG.error("Mini App %s returned an unsuccessful HTTP status (%s)",
                          method, status)
                raise RecoveryError(f"BotFather Mini App {method} returned HTTP {status}.")
            LOG.error("Mini App %s returned an unexpected response shape", method)
            raise RecoveryError(f"BotFather Mini App {method} returned an unexpected response.")
        if status == 400:
            if payload.get("error"):
                # Let _api_request map Bad request and other API errors into
                # their specific retry/fallback paths below.
                return payload
            LOG.warning("Mini App %s returned HTTP 400 without an API error field",
                        method)
            raise MiniAppBadRequest(
                f"BotFather Mini App {method} returned HTTP 400.")
        if not 200 <= status < 300:
            LOG.error("Mini App %s returned an unsuccessful HTTP status (%s)",
                      method, status)
            raise RecoveryError(f"BotFather Mini App {method} returned HTTP {status}.")
        return payload

    def _has_auth_cookie(self) -> bool:
        return any(cookie.name == "stel_bot_father_token" and not cookie.is_expired()
                   for cookie in self.cookies)

    def _clear_auth_cookies(self) -> None:
        for cookie in list(self.cookies):
            if cookie.name in {"stel_ssid", "stel_bot_father_token"}:
                self.cookies.clear(cookie.domain, cookie.path, cookie.name)

    async def _webview_init_data(self) -> str:
        LOG.info("Requesting a fresh BotFather WebView session from Telegram")
        bot_entity = await self.client.get_entity(BOTFATHER)
        peer = await self.client.get_input_entity(BOTFATHER)
        result = await self.client(functions.messages.RequestMainWebViewRequest(
            peer=peer,
            bot=utils.get_input_user(bot_entity),
            platform="tdesktop",
        ))
        url = urlparse(result.url)
        for params in (url.fragment, url.query):
            value = parse_qs(params).get("tgWebAppData", [None])[0]
            if value:
                LOG.info("Telegram returned BotFather WebView session data")
                return value
        raise RecoveryError("Telegram did not include BotFather WebView initData.")

    @staticmethod
    def _hash_from_api_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        parsed = urlparse(value)
        if not parsed.path.rstrip("/").endswith("/api"):
            return None
        candidates = parse_qs(parsed.query).get("hash", [])
        return candidates[0] if candidates and re.fullmatch(
            r"[A-Za-z0-9_-]+", candidates[0]) else None

    async def _refresh_auth(self) -> None:
        LOG.info("Refreshing BotFather Mini App session")
        try:
            await self._refresh_auth_impl()
        except Exception as exc:
            LOG.error("BotFather Mini App session refresh failed (%s)",
                      type(exc).__name__)
            raise
        LOG.info("BotFather Mini App session refreshed successfully")

    async def _refresh_auth_impl(self) -> None:
        me = await self.client.get_me()
        user_id = getattr(me, "id", None)
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise RecoveryError("Could not identify the Telegram account for BotFather auth.")
        init_data = await self._webview_init_data()
        LOG.info("Clearing expired BotFather Mini App authentication state")
        self._clear_auth_cookies()
        self.api_hash = None
        self._save()

        LOG.info("Loading BotFather Mini App page to establish a session")
        status, page, _ = await self._request(BOTFATHER_WEBAPP)
        if status >= 500:
            raise RecoveryError(f"BotFather Mini App page returned HTTP {status}.")
        page = page.replace("\\/", "/")
        match = re.search(r'"apiUrl"\s*:\s*"/api\?hash=([A-Za-z0-9_-]+)', page)
        if not match:
            raise RecoveryError("Could not find BotFather Mini App API hash in its page.")
        api_url = f"{BOTFATHER_WEBAPP}/api?hash={match.group(1)}"
        LOG.info("Authenticating BotFather Mini App session")
        status, body, final_url = await self._request(api_url, {
            "method": "auth", "_auth": init_data,
        })
        error = self._api_error(body)
        if error and error.strip().casefold() == "error":
            LOG.warning("Mini App auth hit BotFather's rate limit")
            raise MiniAppRateLimited("BotFather Mini App auth returned Error.")
        if self._access_denied(status, body, final_url):
            LOG.warning("BotFather Mini App rejected Telegram WebView authentication data")
            raise RecoveryError("BotFather Mini App rejected Telegram initData.")
        payload = self._decode_json(status, body, "auth")
        if payload.get("error"):
            LOG.error("BotFather Mini App authentication was rejected")
            raise RecoveryError("BotFather Mini App authentication was rejected.")
        api_hash = self._hash_from_api_url(payload.get("api_url"))
        if not api_hash:
            raise RecoveryError("BotFather Mini App auth did not return a valid API URL.")
        self.api_hash = api_hash
        if not self._has_auth_cookie():
            raise RecoveryError("BotFather Mini App auth did not set its auth cookie.")
        self.user_id = user_id
        self.identity_checked = True
        self._save()

    async def _api_request(self, method: str, form: dict[str, str]) -> dict[str, Any]:
        if not self.api_hash:
            raise MiniAppAuthExpired("BotFather Mini App API hash is missing.")
        url = f"{BOTFATHER_WEBAPP}/api?hash={self.api_hash}"
        status, body, final_url = await self._request(url, {**form, "method": method})
        error = self._api_error(body)
        if error and error.strip().casefold() == "error":
            LOG.warning("Mini App %s hit BotFather's rate limit", method)
            raise MiniAppRateLimited(f"BotFather Mini App {method} returned Error.")
        if self._access_denied(status, body, final_url):
            LOG.warning("Mini App %s returned an authentication rejection; refreshing session",
                        method)
            raise MiniAppAuthExpired("BotFather Mini App session was rejected or expired.")
        payload = self._decode_json(status, body, method)
        if payload.get("error"):
            error = str(payload["error"])
            if error.casefold() == "bad request":
                LOG.warning("Mini App %s returned Bad request; refreshing session and retrying",
                            method)
                raise MiniAppBadRequest(f"BotFather Mini App {method} returned Bad request.")
            if self._access_denied(status, error, final_url):
                LOG.warning("Mini App %s returned an authentication rejection; refreshing session",
                            method)
                raise MiniAppAuthExpired("BotFather Mini App session was rejected or expired.")
            unavailable = any(term in error.casefold() for term in (
                "taken", "already exists", "not available", "unavailable", "occupied"))
            if method == "createBot" and unavailable:
                LOG.warning("Mini App createBot says the requested username is unavailable")
                raise RecoveryError("BotFather says the requested username is unavailable.")
            if status == 400:
                LOG.warning("Mini App %s returned HTTP 400 after session refresh",
                            method)
                raise MiniAppBadRequest(
                    f"BotFather Mini App {method} returned HTTP 400.")
            # API error text is deliberately omitted: response content could
            # contain identifiers or credential-like values.
            LOG.error("Mini App %s was rejected by BotFather", method)
            raise RecoveryError(f"BotFather Mini App {method} was rejected.")
        LOG.info("Mini App %s completed successfully", method)
        return payload

    async def _authenticated_request(self, method: str,
                                    form: dict[str, str]) -> dict[str, Any]:
        if not self.api_hash or not self._has_auth_cookie() or self.user_id is None:
            LOG.info("Mini App %s needs authentication state; refreshing session", method)
            await self._refresh_auth()
        elif not self.identity_checked:
            LOG.info("Checking which Telegram account owns the cached Mini App session")
            me = await self.client.get_me()
            if getattr(me, "id", None) != self.user_id:
                LOG.warning("Cached Mini App session belongs to a different Telegram account")
                await self._refresh_auth()
            else:
                self.identity_checked = True
                LOG.info("Cached Mini App session belongs to the active Telegram account")
        else:
            LOG.info("Reusing authenticated Mini App session for %s", method)
        try:
            return await self._api_request(method, form)
        except (MiniAppAuthExpired, MiniAppBadRequest) as exc:
            LOG.warning("Retrying Mini App %s after session refresh (%s)",
                        method, type(exc).__name__)
            await self._refresh_auth()
            return await self._api_request(method, form)

    async def create_bot(self, title: str, username: str) -> dict[str, Any]:
        LOG.info("Attempting Mini App bot creation for @%s", username)
        try:
            result = await self._authenticated_request("createBot", {
                "title": title,
                "about": "",
                "username": username,
                "userpic": "",
            })
        except MiniAppRateLimited as exc:
            exc.operation = "create"
            raise
        LOG.info("Mini App bot creation request completed for @%s", username)
        return result

    async def request_transfer(self, bot_handle: str, recipient: str) -> None:
        LOG.info("Preparing Mini App ownership transfer for %s to %s",
                 bot_handle, normalize_handle(recipient))
        bot = await self.client.get_entity(bot_handle)
        owner = await self.client.get_entity(recipient)
        if not getattr(bot, "bot", False):
            raise RecoveryError(f"{bot_handle} is not a Telegram bot.")
        if getattr(owner, "bot", False):
            raise RecoveryError("The new owner must be a Telegram user.")
        try:
            await self._authenticated_request("requestTransferBot", {
                "recipient_id": str(owner.id),
                "bid": str(bot.id),
            })
        except MiniAppRateLimited as exc:
            exc.operation = "transfer"
            raise
        LOG.info("Mini App ownership transfer request submitted for %s", bot_handle)


def transfer_confirmation(message: object, bot_handle: str, recipient: str) -> bool:
    text = response_text(message).casefold()
    return (handle_key(bot_handle) in text and handle_key(recipient) in text
            and any("yes, i am sure" in label.casefold()
                    for label in button_labels(message)))


async def click_botfather_button(message: Any, needle: str,
                                password: str | None = None) -> bool:
    for row, buttons in enumerate(getattr(message, "buttons", None) or ()):
        for column, button in enumerate(buttons):
            label = str(getattr(button, "text", "") or "")
            if needle.casefold() not in label.casefold():
                continue
            required = getattr(getattr(button, "button", None), "requires_password", False)
            if required and password is None:
                raise RecoveryError(
                    "BotFather requires the creator's 2FA password; configure creator.password.")
            while True:
                try:
                    result = await message.click(
                        row, column, **({"password": password} if password is not None else {}))
                    break
                except FloodWaitError as exc:
                    delay = max(1, int(exc.seconds))
                    LOG.warning("BotFather rate limited a button click; retrying in %s seconds", delay)
                    await asyncio.sleep(delay)
            if result is None:
                raise RecoveryError("Telegram did not acknowledge the BotFather button click.")
            callback_text = str(getattr(result, "message", "") or "")
            if "too many attempts" in callback_text.casefold():
                retry_at = time.time() + BOTFATHER_CHAT_COOLDOWN
                LOG.warning("BotFather rate limited a button callback; retrying in %s seconds",
                            BOTFATHER_CHAT_COOLDOWN)
                raise BotFatherRateLimited(
                    "BotFather callback: too many attempts.", retry_at,
                    method="chat")
            return True
    return False


async def botfather_step(client: TelegramClient, source: Any, *,
                         click: str | None = None, send: str | None = None,
                         expected_button: str | None = None,
                         expected_text: Iterable[str] = (),
                         accept_any: bool = False,
                         password: str | None = None) -> Any:
    if (click is None) == (send is None):
        raise ValueError("A BotFather step must click a button or send a message.")
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[Any] = loop.create_future()

    async def receive(event: Any) -> None:
        message = event.message
        if event.chat_id != source.chat_id or message.id < source.id:
            return
        labels = button_labels(message)
        text = response_text(message).casefold()
        button_match = expected_button and any(
            expected_button.casefold() in label.casefold() for label in labels)
        text_match = any(phrase.casefold() in text for phrase in expected_text)
        if (accept_any or button_match or text_match) and not ready.done():
            ready.set_result(message)

    client.add_event_handler(receive, events.NewMessage(chats=BOTFATHER))
    client.add_event_handler(receive, events.MessageEdited(chats=BOTFATHER))
    try:
        if click is not None:
            LOG.info("BotFather chat action: click button %r", click)
            if not await click_botfather_button(source, click, password):
                LOG.error("BotFather did not offer expected button %r", click)
                raise RecoveryError(
                    f"BotFather did not offer {click!r}; buttons: {', '.join(button_labels(source))}")
        else:
            if send and send.startswith("/"):
                LOG.info("BotFather chat action: send command %s", send)
            else:
                LOG.info("BotFather chat action: reply to prompt (message redacted)")
            await client.send_message(BOTFATHER, send)
        await asyncio.sleep(BOTFATHER_ACTION_DELAY)
        try:
            response = await asyncio.wait_for(ready, timeout=60)
        except TimeoutError as exc:
            LOG.error("BotFather did not respond to the last chat action within 60 seconds")
            raise RecoveryError(
                f"BotFather did not respond as expected after {click or 'sending a message'}.") from exc
        if "too many attempts" in response_text(response).casefold():
            retry_at = time.time() + BOTFATHER_CHAT_COOLDOWN
            LOG.warning("BotFather replied that there were too many attempts; retrying in %s seconds",
                        BOTFATHER_CHAT_COOLDOWN)
            raise BotFatherRateLimited(
                "BotFather replied: Sorry, too many attempts.", retry_at,
                method="chat")
        LOG.info("BotFather chat response received for the last action")
        return response
    finally:
        client.remove_event_handler(receive)


async def create_bot_in_chat(client: TelegramClient, title: str, username: str) -> str:
    LOG.info("Starting BotFather chat bot creation for @%s", username)
    before = await client.get_messages(BOTFATHER, limit=1)
    if not before:
        raise RecoveryError("Cannot start BotFather bot creation without chat history.")
    LOG.info("BotFather chat action: send command /newbot")
    prompt = await botfather_step(client, before[0], send="/newbot", accept_any=True)
    LOG.info("BotFather response received for /newbot")
    if "name" not in response_text(prompt).casefold():
        LOG.error("BotFather did not show the expected new-bot name prompt")
        raise RecoveryError("BotFather did not show the new-bot name prompt.")
    prompt = await botfather_step(client, prompt, send=title, accept_any=True)
    if "username" not in response_text(prompt).casefold():
        LOG.error("BotFather did not show the expected new-bot username prompt")
        raise RecoveryError("BotFather did not show the new-bot username prompt.")
    result = await botfather_step(client, prompt, send=username, accept_any=True)
    text = response_text(result)
    folded = text.casefold()
    if any(term in folded for term in (
            "already taken", "taken", "already occupied", "occupied",
            "already exists", "not available", "unavailable")):
        LOG.warning("BotFather says @%s is unavailable", username)
        raise RecoveryError(f"BotFather says username @{username} is taken.")
    if any(term in folded for term in ("error", "invalid", "failed", "sorry")):
        LOG.error("BotFather rejected chat bot creation for @%s", username)
        raise RecoveryError("BotFather rejected the new bot.")
    if TOKEN_RE.search(text) is None and username.casefold() not in folded:
        LOG.error("BotFather did not confirm chat bot creation for @%s", username)
        raise RecoveryError("BotFather did not confirm the new bot creation.")
    LOG.info("BotFather chat confirmed creation of @%s", username)
    return normalize_handle(username)


async def transfer_ownership_in_chat(client: TelegramClient, bot_handle: str,
                                     recipient: str,
                                     password: str | None = None) -> None:
    LOG.info("Starting BotFather chat ownership transfer for %s to %s",
             bot_handle, normalize_handle(recipient))
    before = await client.get_messages(BOTFATHER, limit=1)
    if not before:
        raise RecoveryError("Cannot start a BotFather transfer without chat history.")
    response = await botfather_step(client, before[0], send="/mybots", accept_any=True)
    response = await botfather_step(
        client, response, click=handle_key(bot_handle),
        expected_button="Transfer Ownership")
    response = await botfather_step(
        client, response, click="Transfer Ownership",
        expected_button="Choose recipient")
    response = await botfather_step(
        client, response, click="Choose recipient", accept_any=True)
    response = await botfather_step(
        client, response, send=normalize_handle(recipient),
        expected_button="Yes, I am sure")
    final = await botfather_step(
        client, response, click="Yes, I am sure", accept_any=True,
        password=password)
    validate_transfer_result(final)
    LOG.info("BotFather chat confirmed ownership transfer for %s", bot_handle)


def validate_transfer_result(message: object) -> None:
    text = response_text(message).casefold()
    if any(term in text for term in ("error", "failed", "invalid", "sorry")):
        LOG.error("BotFather rejected ownership transfer")
        raise RecoveryError("BotFather rejected ownership transfer.")
    if not any(term in text for term in (
            "transfer", "ownership", "owned", "success", "done", "worked", "new home")):
        LOG.error("BotFather did not confirm ownership transfer")
        raise RecoveryError("BotFather did not confirm ownership transfer.")


async def _transfer_ownership_via_mini_app(
    client: TelegramClient,
    api: BotFatherMiniApp,
    bot_handle: str,
    recipient: str,
    password: str | None,
) -> None:
    LOG.info("Using Mini App to request ownership transfer of %s to %s",
             bot_handle, normalize_handle(recipient))
    previous = await client.get_messages(BOTFATHER, limit=1)
    if not previous:
        raise RecoveryError("Cannot track BotFather's transfer confirmation without chat history.")
    baseline = previous[0]
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[Any] = loop.create_future()

    async def receive(event: Any) -> None:
        message = event.message
        if (event.chat_id == baseline.chat_id and message.id > baseline.id
                and transfer_confirmation(message, bot_handle, recipient)
                and not ready.done()):
            LOG.info("Received BotFather's ownership confirmation for %s", bot_handle)
            ready.set_result(message)

    client.add_event_handler(receive, events.NewMessage(chats=BOTFATHER))
    client.add_event_handler(receive, events.MessageEdited(chats=BOTFATHER))
    try:
        await api.request_transfer(bot_handle, recipient)
        LOG.info("Waiting for BotFather's ownership confirmation for %s", bot_handle)
        recent = await client.get_messages(BOTFATHER, limit=10)
        confirmation = next((message for message in recent
                             if message.id > baseline.id
                             and transfer_confirmation(message, bot_handle, recipient)), None)
        if confirmation is None:
            try:
                confirmation = await asyncio.wait_for(ready, timeout=60)
            except TimeoutError as exc:
                LOG.error("Timed out waiting for BotFather's ownership confirmation for %s",
                          bot_handle)
                raise RecoveryError(
                    "BotFather did not send the ownership transfer confirmation.") from exc
        final = await botfather_step(
            client, confirmation, click="yes, i am sure",
            expected_text=("worked", "new home", "error", "failed", "invalid", "sorry"),
            password=password)
        validate_transfer_result(final)
        LOG.info("BotFather confirmed ownership transfer for %s", bot_handle)
    finally:
        client.remove_event_handler(receive)


async def transfer_ownership(client: TelegramClient, bot_handle: str, recipient: str, *,
                             password: str | None = None,
                             mini_app: BotFatherMiniApp | None = None,
                             recovery: Recovery | None = None,
                             force_chat: bool = False,
                             allow_chat_fallback: bool = True,
                             allow_mini_app_fallback: bool = False,
                             fallback_retry_at: float | None = None,
                             on_rate_limited: Callable[[str], None] | None = None) -> None:
    api = mini_app or BotFatherMiniApp(client)

    async def transfer_in_chat() -> None:
        try:
            await transfer_ownership_in_chat(client, bot_handle, recipient, password)
        except BotFatherRateLimited as exc:
            exc.method = "chat"
            exc.operation = "transfer"
            if on_rate_limited:
                on_rate_limited("chat")
            raise

    async def transfer_in_mini_app() -> None:
        try:
            await _transfer_ownership_via_mini_app(
                client, api, bot_handle, recipient, password)
        except BotFatherRateLimited as exc:
            exc.operation = "transfer"
            if exc.method == "chat" and on_rate_limited:
                on_rate_limited("chat")
            raise
        except MiniAppRateLimited as exc:
            if on_rate_limited:
                on_rate_limited("mini_app")
            raise BotFatherRateLimited(
                "BotFather Mini App transfer is rate limited.",
                time.time() + BOTFATHER_MINI_APP_COOLDOWN,
                method="mini_app", operation="transfer",
            ) from exc

    if force_chat:
        LOG.info("Using BotFather chat for ownership transfer of %s", bot_handle)
        try:
            await transfer_in_chat()
            return
        except BotFatherRateLimited as chat_error:
            if not allow_mini_app_fallback:
                raise
            LOG.warning("BotFather chat transfer is rate limited; trying Mini App once")
            try:
                await transfer_in_mini_app()
                return
            except BotFatherRateLimited:
                raise
            except MiniAppBadRequest:
                raise chat_error

    try:
        await transfer_in_mini_app()
    except MiniAppBadRequest:
        if not allow_chat_fallback:
            if fallback_retry_at is not None:
                raise BotFatherRateLimited(
                    "BotFather Mini App transfer failed while chat transfer is cooling down.",
                    fallback_retry_at, method="cooldown", operation="transfer")
            raise
        LOG.warning("Mini App transfer was rejected after session refresh; "
                    "switching to BotFather chat for %s", bot_handle)
        await transfer_in_chat()
    except BotFatherRateLimited as mini_app_error:
        # A chat callback limit raised while confirming a Mini App request is
        # already the chat route's limit. Re-entering the chat flow would only
        # repeat the blocked action; persist it and resume after cooldown.
        if mini_app_error.method == "chat" or not allow_chat_fallback:
            raise
        # The Mini App endpoint itself was rate limited. The chat flow has an
        # independent limit, so try that route once and let its limit escape.
        LOG.warning("Mini App transfer is rate limited; trying BotFather chat once")
        await transfer_in_chat()


class BotFather:
    """Per-account BotFather operations. Callers provide per-account locking."""

    def __init__(self, client: TelegramClient, password: str | None = None) -> None:
        self.client = client
        self.password = password
        self.mini_app: BotFatherMiniApp | None = None

    def _app(self) -> BotFatherMiniApp:
        if self.mini_app is None:
            self.mini_app = BotFatherMiniApp(self.client)
        return self.mini_app

    async def create_bot(self, title: str, prefix: str, *, force_chat: bool = False,
                         allow_chat_fallback: bool = True,
                         fallback_retry_at: float | None = None,
                         on_rate_limited: Callable[[str], None] | None = None) -> str:
        use_chat = force_chat
        app = None if force_chat else self._app()

        def unavailable(error: str) -> bool:
            return any(term in error.casefold() for term in (
                "taken", "already exists", "not available", "unavailable", "occupied"))

        for digits in (2, 3, 4):
            if len(prefix) + digits + 3 > 32:
                continue
            number = secrets.randbelow(9 * 10 ** (digits - 1)) + 10 ** (digits - 1)
            candidate = f"{prefix}{number}bot"
            operation = "chat" if use_chat else "Mini App"
            LOG.info("Attempting backup bot creation for @%s via %s", candidate, operation)
            if use_chat:
                try:
                    handle = await create_bot_in_chat(self.client, title, candidate)
                    LOG.info("BotFather created backup bot %s via chat", handle)
                    return handle
                except BotFatherRateLimited as exc:
                    exc.method = "chat"
                    exc.operation = "create"
                    raise
                except RecoveryError as exc:
                    if unavailable(str(exc)):
                        LOG.warning("BotFather says @%s is unavailable; trying another username",
                                    candidate)
                        continue
                    LOG.error("BotFather chat creation failed for @%s (%s)",
                              candidate, type(exc).__name__)
                    raise RecoveryError(f"BotFather chat creation failed for @{candidate}: {exc}") from exc
            try:
                result = await app.create_bot(title, candidate)  # type: ignore[union-attr]
            except MiniAppRateLimited as exc:
                LOG.warning("Mini App creation for @%s was rate limited",
                            candidate)
                if on_rate_limited:
                    on_rate_limited("mini_app")
                if not allow_chat_fallback:
                    raise BotFatherRateLimited(
                        "BotFather Mini App creation is rate limited.",
                        time.time() + BOTFATHER_MINI_APP_COOLDOWN,
                        method="mini_app", operation="create",
                    ) from exc
                use_chat = True
                try:
                    return await create_bot_in_chat(self.client, title, candidate)
                except BotFatherRateLimited as chat_exc:
                    chat_exc.method = "chat"
                    chat_exc.operation = "create"
                    raise
                except RecoveryError as exc:
                    if unavailable(str(exc)):
                        LOG.warning("BotFather says @%s is unavailable; trying another username",
                                    candidate)
                        continue
                    LOG.error("BotFather chat fallback failed for @%s (%s)",
                              candidate, type(exc).__name__)
                    raise RecoveryError(f"BotFather chat creation failed for @{candidate}: {exc}") from exc
            except MiniAppBadRequest as exc:
                if not allow_chat_fallback and fallback_retry_at is not None:
                    raise BotFatherRateLimited(
                        "BotFather Mini App creation failed while chat creation is cooling down.",
                        fallback_retry_at, method="cooldown", operation="create",
                    ) from exc
                raise
            except RecoveryError as exc:
                if unavailable(str(exc)):
                    LOG.warning("BotFather says @%s is unavailable; trying another username",
                                candidate)
                    continue
                LOG.error("Mini App creation failed for @%s (%s)",
                          candidate, type(exc).__name__)
                raise RecoveryError(f"BotFather rejected @{candidate}: {exc}") from exc
            if result.get("ok") is True:
                bot_id = result.get("bot_id")
                if isinstance(bot_id, int) and not isinstance(bot_id, bool) and bot_id > 0:
                    LOG.info("BotFather created backup bot @%s via Mini App", candidate)
                    return normalize_handle(candidate)
                raise RecoveryError("BotFather Mini App created a bot without returning its ID.")
            error = str(result.get("msg") or result.get("error") or "unknown error")
            if unavailable(error):
                LOG.warning("BotFather says @%s is unavailable; trying another username",
                            candidate)
                continue
            LOG.error("Mini App createBot returned an unsuccessful result for @%s", candidate)
            raise RecoveryError("BotFather rejected backup bot creation.")
        raise RecoveryError(f"Could not find an available {prefix}<number>bot username.")

    async def transfer(self, bot_handle: str, recipient: str, *,
                       recovery: Recovery | None = None,
                       force_chat: bool = False,
                       allow_chat_fallback: bool = True,
                       allow_mini_app_fallback: bool = False,
                       fallback_retry_at: float | None = None,
                       on_rate_limited: Callable[[str], None] | None = None) -> None:
        await transfer_ownership(
            self.client, bot_handle, recipient, password=self.password,
            mini_app=self._app(),
            recovery=recovery, force_chat=force_chat,
            allow_chat_fallback=allow_chat_fallback,
            allow_mini_app_fallback=allow_mini_app_fallback,
            fallback_retry_at=fallback_retry_at,
            on_rate_limited=on_rate_limited)

    async def set_info(self, target: str, *, name: str, about: str,
                       description: str) -> None:
        for command, value in (("/setname", name), ("/setabouttext", about),
                               ("/setdescription", description)):
            await self._set_text_field(target, command, value)

    async def set_about(self, target: str, value: str) -> None:
        await self._set_text_field(target, "/setabouttext", value)

    async def set_description(self, target: str, value: str) -> None:
        await self._set_text_field(target, "/setdescription", value)

    async def _set_text_field(self, target: str, command: str, value: str) -> None:
        async with self.client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
            await conv.send_message("/cancel")
            await conv.get_response()
            await conv.send_message(command)
            await conv.get_response()
            await conv.send_message(target)
            await conv.get_response()
            clean = value.replace("\r\n", "\n").replace("\r", "\n")
            await conv.send_message(clean or "/empty")
            reply = await conv.get_response()
        if "error" in response_text(reply).casefold():
            raise RecoveryError(f"BotFather rejected {command}: {response_text(reply)}")

    async def set_commands(self, target: str, commands: list[types.BotCommand]) -> None:
        payload = "\n".join(f"{item.command} - {item.description}" for item in commands) or "/empty"
        async with self.client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
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

    async def regenerate_token(self, target: str) -> str:
        async with self.client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
            await conv.send_message("/cancel")
            await conv.get_response()
            await conv.send_message("/revoke")
            await conv.get_response()
            await conv.send_message(target)
            response = await conv.get_response()
        token = TOKEN_RE.search(response_text(response))
        if not token:
            raise RecoveryError("BotFather did not return a replacement token.")
        return token.group(0)

    async def owned_handles(self) -> set[str]:
        async with self.client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
            await conv.send_message("/cancel")
            await conv.get_response()
            await conv.send_message("/mybots")
            response = await conv.get_response()
        return {handle_key(match.group(1))
                for label in button_labels(response)
                for match in HANDLE_RE.finditer(label)}

    async def get_source_commands(self, source: str,
                                  api_id: int, api_hash: str) -> list[types.BotCommand]:
        async with self.client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
            await conv.send_message("/cancel")
            await conv.get_response()
            await conv.send_message("/token")
            await conv.get_response()
            await conv.send_message(source)
            response = await conv.get_response()
        token = TOKEN_RE.search(response_text(response))
        if not token:
            raise RecoveryError(f"Could not read the source token for {source}.")
        bot_client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await bot_client.start(bot_token=token.group(0))
            result = await bot_client(functions.bots.GetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code=""))
            return list(result)
        finally:
            await bot_client.disconnect()


def make_client(account_config: Account, api_id: int, api_hash: str,
                *, flood_sleep_threshold: int = 60) -> TelegramClient:
    return TelegramClient(str(account_config.session), api_id, api_hash,
                          flood_sleep_threshold=flood_sleep_threshold)


async def interact_with_bot(client: TelegramClient, handle: str) -> None:
    entity = await client.get_entity(handle)
    if not getattr(entity, "bot", False):
        raise RecoveryError(f"{handle} is not a Telegram bot.")
    await client.send_message(handle, "/start")


def snapshot_path(config: Config, bot: BotSpec) -> Path:
    return config.snapshot_dir / bot.prefix.casefold() / "snapshot.json"


def read_snapshot(config: Config, bot: BotSpec) -> dict[str, Any]:
    path = snapshot_path(config, bot)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"Could not read bot snapshot {path}: {exc}") from exc
    required = ("source_handle", "name", "about", "description", "commands")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise RecoveryError(f"Bot snapshot {path} is incomplete.")
    if not all(isinstance(value[key], str) for key in required[:-1]):
        raise RecoveryError(f"Bot snapshot {path} contains invalid profile text.")
    if not isinstance(value["commands"], list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("command"), str)
            and isinstance(item.get("description"), str)
            for item in value["commands"]):
        raise RecoveryError(f"Bot snapshot {path} contains invalid commands.")
    photo_name = value.get("profile_photo")
    if photo_name is not None:
        if not isinstance(photo_name, str) or Path(photo_name).name != photo_name:
            raise RecoveryError(f"Bot snapshot {path} has an invalid photo name.")
        if not (path.parent / photo_name).is_file():
            raise RecoveryError(f"Snapshot profile photo is missing: {path.parent / photo_name}")
    return value


async def create_snapshot(client: TelegramClient, botfather: BotFather,
                          config: Config, bot: BotSpec) -> None:
    if not bot.handle:
        raise RecoveryError(
            f"No snapshot exists for {bot.friendly_name}; configure its current handle first.")
    source = await client.get_input_entity(bot.handle)
    entity = await client.get_entity(source)
    if not getattr(entity, "bot", False):
        raise RecoveryError(f"Snapshot source {bot.handle} is not a bot.")
    info = await client(functions.bots.GetBotInfoRequest(lang_code="", bot=source))
    commands = await botfather.get_source_commands(
        bot.handle, config.api_id, config.api_hash)
    directory = snapshot_path(config, bot).parent
    directory.mkdir(parents=True, exist_ok=True)
    photo = await client.download_profile_photo(
        entity, file=str(directory / "profile-photo"), download_big=True)
    snapshot = {
        "source_handle": bot.handle,
        "name": entity.first_name or "",
        "about": info.about or "",
        "description": info.description or "",
        "commands": [{"command": item.command, "description": item.description}
                     for item in commands],
        "profile_photo": Path(photo).name if photo else None,
    }
    atomic_write_text(snapshot_path(config, bot), json.dumps(
        snapshot, indent=2, ensure_ascii=False) + "\n")
    try:
        os.chmod(directory, 0o700)
        if photo:
            os.chmod(photo, 0o600)
    except OSError:
        LOG.warning("Could not set restrictive permissions on snapshot files")


async def restore_snapshot(client: TelegramClient, botfather: BotFather,
                           config: Config, bot: BotSpec,
                           target_handle: str) -> str:
    snapshot = read_snapshot(config, bot)
    await botfather.set_info(
        target_handle,
        name=bot_handle_replacer(bot.prefix, target_handle, snapshot["name"]),
        about=bot_handle_replacer(bot.prefix, target_handle, snapshot["about"]),
        description=bot_handle_replacer(bot.prefix, target_handle, snapshot["description"]),
    )
    photo_name = snapshot.get("profile_photo")
    if photo_name:
        photo = snapshot_path(config, bot).parent / photo_name
        target = await client.get_entity(target_handle)
        if (not getattr(target, "bot", False) or getattr(target, "deleted", False)
                or getattr(target, "access_hash", None) is None):
            raise RecoveryError(f"Telegram does not recognize {target_handle} as a valid bot.")
        uploaded = await client.upload_file(str(photo))
        await client(functions.photos.UploadProfilePhotoRequest(
            bot=types.InputUser(target.id, target.access_hash), file=uploaded))
    try:
        commands = [types.BotCommand(
            command=item["command"],
            description=bot_handle_replacer(bot.prefix, target_handle, item["description"]))
            for item in snapshot["commands"]]
    except (KeyError, TypeError) as exc:
        raise RecoveryError("The saved bot command snapshot is invalid.") from exc
    await botfather.set_commands(target_handle, commands)
    return await botfather.regenerate_token(target_handle)


async def anonymous_link(client: TelegramClient) -> str:
    async with client.conversation(ANONYMOUS_BOT, timeout=45, exclusive=True) as conv:
        await conv.send_message("/start")
        await conv.get_response()
        await conv.send_message("/link")
        response = await conv.get_response()
    match = ANON_LINK_RE.search(response_text(response))
    if not match:
        raise RecoveryError("IncogNoteBot did not return the expected unique link.")
    return match.group(0)


async def revoke_anonymous_link(client: TelegramClient) -> None:
    async with client.conversation(ANONYMOUS_BOT, timeout=45, exclusive=True) as conv:
        await conv.send_message("/change")
        await conv.get_response()
        await conv.send_message("Yes, I am sure")
        response = await conv.get_response()
    if any(word in response_text(response).casefold()
           for word in ("error", "failed", "invalid")):
        raise RecoveryError("IncogNoteBot did not confirm link revocation: "
                            + response_text(response))


async def bot_needs_recovery(client: TelegramClient, handle: str) -> bool:
    try:
        entity = await client.get_entity(handle)
        if not getattr(entity, "bot", False) or getattr(entity, "deleted", False):
            return True
        peer = await client.get_input_entity(entity)
        result = await client(functions.messages.GetPeerSettingsRequest(peer))
        settings = getattr(result, "settings", result)
        if getattr(settings, "blocked", False):
            return True
        async with client.conversation(handle, timeout=45, exclusive=True) as conv:
            sent = await conv.send_message("/about")
            await conv.get_response(sent)
        return False
    except (asyncio.TimeoutError, OSError, RPCError, ValueError):
        return True


async def update_bot_profile_references(
    client: TelegramClient,
    botfather: BotFather,
    handles: Iterable[str],
    prefix: str,
    new_handle: str,
) -> list[str]:
    seen: set[str] = set()
    active_handles: list[str] = []
    for handle in handles:
        key = handle_key(handle)
        if not key or key in seen:
            continue
        seen.add(key)
        is_replacement = key == handle_key(new_handle)
        try:
            entity = await client.get_entity(handle)
        except (UsernameInvalidError, UsernameNotOccupiedError) as exc:
            if is_replacement:
                raise RecoveryError(
                    f"Replacement bot @{key} could not be resolved during finalization.") from exc
            LOG.warning("Skipping profile updates for configured bot @%s: "
                        "Telegram says the username is invalid or unused",
                        key)
            continue
        if not getattr(entity, "bot", False) or getattr(entity, "deleted", False):
            if is_replacement:
                raise RecoveryError(
                    f"Replacement profile target @{key} is not an active bot.")
            LOG.warning("Skipping profile updates for configured profile target @%s: "
                        "it is not an active bot", key)
            continue
        active_handles.append(handle)
        peer = await client.get_input_entity(entity)
        info = await client(functions.bots.GetBotInfoRequest(lang_code="", bot=peer))
        old_about = str(getattr(info, "about", "") or "")
        old_description = str(getattr(info, "description", "") or "")
        about = bot_handle_replacer(prefix, new_handle, old_about)
        description = bot_handle_replacer(prefix, new_handle, old_description)
        if about == old_about and description == old_description:
            continue
        if about != old_about:
            await botfather.set_about(handle, about)
        if description != old_description:
            await botfather.set_description(handle, description)
        LOG.info("Updated BotFather profile references to %s in %s", new_handle, handle)
    return active_handles


async def update_runtime_about(client: TelegramClient, target_handle: str,
                               prefix: str, new_handle: str) -> None:
    async with client.conversation(target_handle, timeout=90, exclusive=True) as conv:
        sent = await conv.send_message("/about")
        current = await conv.get_response(sent)
        old_text = response_text(current)
        updated = bot_handle_replacer(prefix, new_handle, old_text)
        if updated == old_text:
            return
        sent = await conv.send_message("/about " + updated)
        result = await conv.get_response(sent)
        if any(term in response_text(result).casefold()
               for term in ("error", "invalid", "admin only", "failed")):
            raise RecoveryError(
                f"{target_handle} rejected its /about update: {response_text(result)}")
        sent = await conv.send_message("/reload")
        result = await conv.get_response(sent)
        if any(term in response_text(result).casefold()
               for term in ("error", "invalid", "admin only", "failed")):
            raise RecoveryError(f"{target_handle} rejected /reload: {response_text(result)}")


async def update_chat_description(client: TelegramClient, chat: str,
                                  prefix: str, handle: str) -> None:
    full = await client(functions.channels.GetFullChannelRequest(channel=chat))
    current = str(getattr(getattr(full, "full_chat", None), "about", "") or "")
    updated = bot_handle_replacer(prefix, handle, current)
    if updated == current:
        return
    try:
        await client(functions.messages.EditChatAboutRequest(peer=chat, about=updated))
    except ChatAboutNotModifiedError:
        pass


def update_page_html(html: str, bot: BotSpec, handle: str) -> str:
    label = f"Updated: {date.today().isoformat()} (Restored {bot.friendly_name})"
    updated, count = re.subn(r"Updated:\s*[^<\r\n]+", label, html,
                             count=1, flags=re.IGNORECASE)
    if count == 0:
        LOG.warning("No Updated: label found in the public page")
    return bot_handle_replacer(bot.prefix, handle, updated)


async def publish_page(config: Config, bot: BotSpec, handle: str) -> None:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(config.pages_url) as response:
            if response.status >= 400:
                raise RecoveryError(f"Could not fetch Pages index ({response.status}).")
            html = await response.text()
    content = update_page_html(html, bot, handle)
    with tempfile.TemporaryDirectory(prefix="forward-bot-pages-") as temp_dir:
        Path(temp_dir, "index.html").write_text(content, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            "npx", "wrangler", "pages", "deploy", temp_dir,
            "--project-name", config.pages_project,
            "--env-file", ".env",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = (stderr or stdout).decode(errors="replace").strip()
            raise RecoveryError("Wrangler Pages deploy failed: " + detail)


class RecoveryService:
    """Coordinates monitoring, ownership changes, and bot restoration."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.state_path)
        self.main_client = make_client(config.main, config.api_id, config.api_hash)
        self.pool_clients = {
            item.username.casefold(): make_client(item, config.api_id, config.api_hash)
            for item in config.pool
        }
        self.creator_client = (
            make_client(config.creator, config.creator_api_id or config.api_id,
                        config.creator_api_hash or config.api_hash,
                        flood_sleep_threshold=0)
            if config.creator else None
        )
        self.clients: dict[str, TelegramClient] = {
            config.main.username.casefold(): self.main_client,
            **self.pool_clients,
        }
        if self.creator_client is not None and config.creator is not None:
            self.clients[config.creator.username.casefold()] = self.creator_client
        self.botfather: dict[str, BotFather] = {
            config.main.username.casefold(): BotFather(self.main_client),
            **{key: BotFather(client) for key, client in self.pool_clients.items()},
        }
        if self.creator_client is not None and config.creator is not None:
            self.botfather[config.creator.username.casefold()] = BotFather(
                self.creator_client, config.creator_password)

        account_keys = list(self.clients)
        self.anonymous_locks = {key: asyncio.Lock() for key in account_keys}
        self.botfather_locks = {key: asyncio.Lock() for key in account_keys}
        self.recovery_locks: dict[str, asyncio.Lock] = {}
        self.donation_locks: dict[str, asyncio.Lock] = {}
        self.backup_locks = {bot.prefix.casefold(): asyncio.Lock() for bot in config.bots}
        self.state_lock = asyncio.Lock()
        self.config_write_lock = asyncio.Lock()
        self.description_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        self.creator_client_available = False
        self.pool_available: set[str] = set()
        self.monitor_tasks: list[asyncio.Task[Any]] = []

    @property
    def state(self) -> RuntimeState:
        return self.store.state

    def _persist(self) -> None:
        self.store.save()

    async def run(self) -> None:
        self.store.load()
        self._migrate_legacy_botfather_cooldowns()
        await self._start_client(self.main_client, self.config.main)
        if self.creator_client is not None and self.config.creator is not None:
            try:
                await self._start_client(self.creator_client, self.config.creator)
                self.creator_client_available = self.creator_client.is_connected()
            except Exception as exc:
                LOG.error("Creator account %s is unavailable: %s",
                          self.config.creator.username, exc)
        for account_config in self.config.pool:
            key = account_config.username.casefold()
            try:
                await self._start_client(self.pool_clients[key], account_config)
                if self.pool_clients[key].is_connected():
                    self.pool_available.add(key)
            except Exception as exc:
                LOG.error("Pool account %s is unavailable: %s", account_config.username, exc)

        self._register_handlers()
        await self._cleanup_unretained_anonymous_links()
        await self._ensure_snapshots()
        await self._reconcile_prepared_bots()
        await self._check_configured_bots()
        await self._resume_recoveries()
        await self._resume_backup_donations()
        await self._ensure_prepared_bots()
        LOG.info("Recovery service started as %s", self.config.main.username)

        self.monitor_tasks = [
            asyncio.create_task(self._deleted_bot_monitor(), name="deleted-bot-monitor"),
            asyncio.create_task(self._botfather_retry_monitor(), name="botfather-retry-monitor"),
        ]
        clients_to_run = [self.main_client, *(
            self.pool_clients[key] for key in self.pool_available
            if self.pool_clients[key].is_connected())]
        try:
            await asyncio.gather(*(client.run_until_disconnected()
                                   for client in clients_to_run))
        finally:
            for task in self.monitor_tasks:
                task.cancel()
            await asyncio.gather(*self.monitor_tasks, return_exceptions=True)
            await asyncio.gather(*(client.disconnect() for client in self.clients.values()),
                                 return_exceptions=True)

    async def _start_client(self, client: TelegramClient, account_config: Account) -> None:
        account_config.session.parent.mkdir(parents=True, exist_ok=True)
        await client.start()
        me = await client.get_me()
        actual = normalize_handle(getattr(me, "username", None) or "")
        if actual.casefold() != account_config.username.casefold():
            raise RecoveryError(
                f"Session {account_config.session} belongs to {actual}, "
                f"expected {account_config.username}.")

    def _register_handlers(self) -> None:
        self.main_client.add_event_handler(
            self._on_main_message, events.NewMessage(incoming=True))
        for username, client in self.pool_clients.items():
            if username not in self.pool_available:
                continue
            async def handle(event: Any, receiver: str = username) -> None:
                await self._on_pool_message(receiver, event)
            client.add_event_handler(handle, events.NewMessage(incoming=True))

    async def _on_main_message(self, event: events.NewMessage.Event) -> None:
        try:
            sender = await event.get_sender()
            username = str(getattr(sender, "username", "") or "").casefold()
            if username == ABUSE_SENDER.casefold():
                await self._start_from_abuse(event.message)
            elif username == ANONYMOUS_BOT.lstrip("@").casefold():
                await self._on_anonymous_message(
                    self.config.main.username.casefold(), self.main_client, event)
            elif (username == BOTFATHER.casefold()
                  and OWNERSHIP_PHRASE in response_text(event.message).casefold()):
                await self._on_main_ownership(event.message)
        except Exception:
            LOG.exception("Error handling an incoming main-account message")

    async def _on_pool_message(self, receiver: str,
                               event: events.NewMessage.Event) -> None:
        try:
            sender = await event.get_sender()
            username = str(getattr(sender, "username", "") or "").casefold()
            if username == ANONYMOUS_BOT.lstrip("@").casefold():
                await self._on_anonymous_message(receiver, self.pool_clients[receiver], event)
            elif (username == BOTFATHER.casefold()
                  and OWNERSHIP_PHRASE in response_text(event.message).casefold()):
                await self._on_pool_ownership(receiver, event.message)
        except Exception:
            LOG.exception("Error handling an incoming message for pool %s", receiver)

    async def _ensure_snapshots(self) -> None:
        for bot in self.config.bots:
            path = snapshot_path(self.config, bot)
            if path.exists():
                read_snapshot(self.config, bot)
                continue
            async with self.botfather_locks[self.config.main.username.casefold()]:
                await create_snapshot(
                    self.main_client,
                    self.botfather[self.config.main.username.casefold()],
                    self.config,
                    bot,
                )
            LOG.info("Created settings snapshot for %s", bot.friendly_name)

    async def _check_configured_bots(self) -> None:
        async def check(bot: BotSpec) -> None:
            if bot.handle and await bot_needs_recovery(self.main_client, bot.handle):
                LOG.warning("Configured bot %s (%s) is unavailable; starting recovery",
                            bot.friendly_name, bot.handle)
                await self._start_recovery(bot, bot.handle)
        await asyncio.gather(*(check(bot) for bot in self.config.bots))

    async def _deleted_bot_monitor(self) -> None:
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            try:
                await self._check_deleted_bots()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Configured-bot deletion check failed")

    async def _check_deleted_bots(self) -> None:
        async def check(bot: BotSpec) -> None:
            if not bot.handle:
                return
            async with self.state_lock:
                if bot.prefix.casefold() in self.state.recoveries:
                    return
            try:
                entity = await self.main_client.get_entity(bot.handle)
            except (UsernameInvalidError, UsernameNotOccupiedError):
                entity = None
            except (asyncio.TimeoutError, OSError, RPCError, ValueError):
                return
            if entity is not None and not getattr(entity, "deleted", False):
                return
            LOG.warning("Configured bot %s (%s) is deleted; starting recovery",
                        bot.friendly_name, bot.handle)
            await self._start_recovery(bot, bot.handle)
        await asyncio.gather(*(check(bot) for bot in self.config.bots))

    def _available_receiver(self) -> str | None:
        if not self.config.pool:
            key = self.config.main.username.casefold()
            return key if self.main_client.is_connected() else None
        candidates = [key for key, client in self.pool_clients.items()
                      if key in self.pool_available and client.is_connected()]
        if not candidates:
            return None
        return min(candidates, key=lambda key: sum(
            recovery.receiver.casefold() == key
            for recovery in self.state.recoveries.values()))

    def _client_for(self, username: str) -> TelegramClient:
        try:
            return self.clients[username.casefold()]
        except KeyError as exc:
            raise RecoveryError(f"No Telegram client is configured for {username}.") from exc

    def _receiver_display_name(self, username: str) -> str:
        if username.casefold() == self.config.main.username.casefold():
            return self.config.main.username
        account_config = next((item for item in self.config.pool
                               if item.username.casefold() == username.casefold()), None)
        if account_config is None:
            raise RecoveryError(f"Unknown recovery receiver {username}.")
        return account_config.username

    async def _start_from_abuse(self, message: object) -> None:
        handles = message_handles(message)
        if not handles:
            LOG.warning("Abuse notification contained no bot username: %s", response_text(message))
            return
        jobs = []
        for handle in handles:
            bot = self.config.mapping_for_handle(handle)
            if bot:
                jobs.append(self._start_recovery(bot, handle))
        if jobs:
            await asyncio.gather(*jobs)

    async def _start_recovery(self, bot: BotSpec, old_handle: str) -> None:
        key = bot.prefix.casefold()
        async with self.state_lock:
            if key in self.state.recoveries:
                LOG.info("Recovery for %s is already active", bot.friendly_name)
                return
            has_spare = bool(self.state.backups.get(key))
            if self.config.automatic_enabled or has_spare:
                receiver = self.config.main.username.casefold()
                stage = "automatic_preparing"
                mode = "automatic"
            else:
                receiver = self._available_receiver()
                if receiver is None:
                    LOG.error("No available receiving account for %s", bot.friendly_name)
                    return
                stage = "starting"
                mode = "manual"
            recovery = Recovery(
                key=key,
                old_handle=normalize_handle(old_handle),
                receiver=receiver,
                stage=stage,
                reward_code=reward_code() if mode == "manual" else "",
                mode=mode,
            )
            self.state.recoveries[key] = recovery
            self._persist()
        LOG.info("Starting %s restoration for %s; trying BotFather Mini App first",
                 mode, bot.friendly_name)
        try:
            if mode == "automatic":
                await self._run_automatic_or_fallback(recovery, bot)
            else:
                await self._prepare_manual_recovery(recovery, bot)
        except BotFatherRateLimited:
            LOG.warning("Recovery for %s is paused until the BotFather cooldown ends",
                        bot.friendly_name)
        except Exception:
            LOG.exception("Could not start recovery for %s", bot.friendly_name)

    async def _get_or_create_anonymous_link(
            self, receiver: str, preferred: str = "") -> str:
        receiver = receiver.casefold()
        async with self.anonymous_locks[receiver]:
            async with self.state_lock:
                link = preferred or self.state.anonymous_links.get(receiver, "")
            if not link:
                link = await anonymous_link(self._client_for(receiver))
            async with self.state_lock:
                self.state.anonymous_links[receiver] = link
                self._persist()
            return link

    async def _cleanup_unretained_anonymous_links(self) -> None:
        if self.config.retain_anonymous_link:
            return
        for receiver, link in list(self.state.anonymous_links.items()):
            async with self.state_lock:
                active_manual = any(
                    item.receiver.casefold() == receiver and item.mode == "manual"
                    for item in self.state.recoveries.values()
                )
            if active_manual:
                continue
            async with self.anonymous_locks[receiver]:
                try:
                    await revoke_anonymous_link(self._client_for(receiver))
                except Exception:
                    LOG.exception("Could not revoke unretained anonymous link for %s", receiver)
                    continue
                async with self.state_lock:
                    if self.state.anonymous_links.get(receiver) == link:
                        self.state.anonymous_links.pop(receiver, None)
                        self._persist()
                LOG.info("Revoked saved anonymous link for %s because retention is disabled",
                         receiver)

    async def _prepare_manual_recovery(self, recovery: Recovery, bot: BotSpec) -> None:
        receiver = recovery.receiver.casefold()
        link = await self._get_or_create_anonymous_link(receiver, recovery.anonymous_link)
        async with self.state_lock:
            if self.state.recoveries.get(recovery.key) is not recovery:
                return
            recovery.anonymous_link = link
            self._persist()
        if recovery.announcement_message_id is not None:
            return
        announcement_text = render(
            self.config.recovery_template,
            friendly_name=bot.friendly_name,
            old_handle=recovery.old_handle,
            handle_prefix=bot.prefix,
            anonymous_link=recovery.anonymous_link,
        )
        # Recover a send that succeeded immediately before a process crash.
        existing = await self.main_client.get_messages(
            self.config.announcement_channel, limit=50)
        announcement = next((item for item in existing
                             if response_text(item) == announcement_text), None)
        if announcement is None:
            announcement = await self.main_client.send_message(
                self.config.announcement_channel, announcement_text)
        message_id = getattr(announcement, "id", None)
        if not isinstance(message_id, int):
            raise RecoveryError("Could not record the recovery announcement message ID.")
        async with self.state_lock:
            if self.state.recoveries.get(recovery.key) is not recovery:
                return
            recovery.announcement_message_id = message_id
            recovery.stage = "waiting_submission"
            self._persist()
        LOG.info("Requested replacement for %s", bot.friendly_name)

    @staticmethod
    def _valid_candidate(bot: BotSpec, handle: str) -> bool:
        clean = handle.lstrip("@").casefold()
        return bool(re.fullmatch(re.escape(bot.prefix.casefold()) + r"\d+bot", clean))

    @staticmethod
    def _matching_candidate(recovery: Recovery, handles: set[str]) -> str | None:
        candidates = recovery.candidate_handles or (
            [recovery.new_handle] if recovery.new_handle else [])
        found = [candidate for candidate in candidates if any(
            handle_key(candidate) == handle_key(value) for value in handles)]
        return found[0] if len(found) == 1 else None

    async def _on_anonymous_message(self, receiver: str, client: TelegramClient,
                                    event: events.NewMessage.Event) -> None:
        if not any("block" in label.casefold() for label in button_labels(event.message)):
            return
        incoming_handles = message_handles(event.message)
        async with self.state_lock:
            pending = [item for item in self.state.recoveries.values()
                       if item.receiver.casefold() == receiver
                       and item.stage in {"waiting_submission", "waiting_main", "waiting_pool"}]
            matching: list[tuple[Recovery, BotSpec, list[str]]] = []
            for recovery in pending:
                bot = self.config.mapping_for_key(recovery.key)
                valid = [handle for handle in incoming_handles
                         if self._valid_candidate(bot, handle)]
                if valid:
                    matching.append((recovery, bot, valid))
        if len(matching) == 1:
            recovery, bot, candidates = matching[0]
        elif len(matching) > 1:
            LOG.warning("Ambiguous anonymous replacement message: %s",
                        response_text(event.message))
            return
        elif len(pending) == 1:
            # A pending recovery owns submissions matching its prefix. A bot
            # for another configured prefix can still be donated as a spare.
            donation = self._backup_donation_candidate(incoming_handles, pending)
            if donation and self.config.retain_anonymous_link:
                await self._accept_backup_donation(
                    receiver, client, event, donation[0], donation[1])
                return
            recovery = pending[0]
            bot = self.config.mapping_for_key(recovery.key)
            candidates = []
        else:
            donation = self._backup_donation_candidate(incoming_handles, pending)
            if donation and self.config.retain_anonymous_link:
                await self._accept_backup_donation(
                    receiver, client, event, donation[0], donation[1])
            return
        if len(candidates) != 1:
            await event.reply(
                f"Your message does not contain a valid bot. Create a bot named "
                f"{bot.prefix}<a random short number>bot and reply to this message "
                "with its @username here.")
            return
        candidate = candidates[0]
        async with self._recovery_lock(recovery.key):
            async with self.state_lock:
                current = self.state.recoveries.get(recovery.key)
                if current is not recovery or recovery.stage not in {
                        "waiting_submission", "waiting_main", "waiting_pool"}:
                    return
            try:
                await interact_with_bot(client, candidate)
            except (asyncio.TimeoutError, OSError, RPCError, ValueError):
                await event.reply(
                    f"Your message does not contain a valid bot. Create a bot named "
                    f"{bot.prefix}<a random short number>bot and reply to this message "
                    "with its @username here.")
                return
            async with self.state_lock:
                if self.state.recoveries.get(recovery.key) is not recovery:
                    return
                if all(handle_key(value) != handle_key(candidate)
                       for value in recovery.candidate_handles):
                    recovery.candidate_handles.append(candidate)
                recovery.candidate_message_ids[handle_key(candidate)] = event.message.id
                recovery.new_handle = candidate
                recovery.anonymous_message_id = event.message.id
                recovery.stage = ("waiting_main" if receiver == self.config.main.username.casefold()
                                  else "waiting_pool")
                self._persist()
        await event.reply(
            f"Thank you. Transfer {candidate} to {self._receiver_display_name(receiver)}.")

    def _backup_donation_candidate(
            self, handles: set[str],
            pending: list[Recovery]) -> tuple[BotSpec, str] | None:
        pending_keys = {item.key.casefold() for item in pending}
        found: list[tuple[BotSpec, str]] = []
        for handle in handles:
            bot = self.config.mapping_for_handle(handle)
            if bot is None or bot.prefix.casefold() in pending_keys:
                continue
            if self._valid_candidate(bot, handle):
                found.append((bot, handle))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            LOG.warning("Anonymous message contains multiple possible backup bots: %s",
                        ", ".join(item[1] for item in found))
        return None

    async def _accept_backup_donation(
            self, receiver: str, client: TelegramClient,
            event: events.NewMessage.Event, bot: BotSpec, handle: str) -> None:
        if not self.config.retain_anonymous_link:
            return
        if bot.handle and handle_key(handle) == handle_key(bot.handle):
            await event.reply("That is the bot currently in service, so it cannot be donated as a spare.")
            return
        try:
            await interact_with_bot(client, handle)
        except (asyncio.TimeoutError, OSError, RPCError, ValueError):
            await event.reply(
                f"That does not appear to be an active bot. Send a valid @{bot.prefix}<number>bot username.")
            return

        donation_key = handle_key(handle)
        bot_key = bot.prefix.casefold()
        async with self.backup_locks[bot_key]:
            async with self.state_lock:
                if any(handle_key(item) == donation_key
                       for item in self.state.backups.get(bot_key, [])):
                    already_saved = True
                else:
                    already_saved = False
                existing = self.state.backup_donations.get(donation_key)
                active_recovery_handle = any(
                    handle_key(value) == donation_key
                    for recovery in self.state.recoveries.values()
                    for value in ([recovery.new_handle] if recovery.new_handle else [])
                    + recovery.candidate_handles
                )
                pending_creator_handle = any(
                    handle_key(value) == donation_key
                    for value in self.state.pending_creations.values()
                )
                current_handle = bool(
                    bot.handle and handle_key(bot.handle) == donation_key)
                in_use = active_recovery_handle or pending_creator_handle or current_handle
                if not already_saved and existing is None and not in_use:
                    self.state.backup_donations[donation_key] = BackupDonation(
                        handle=normalize_handle(handle),
                        key=bot_key,
                        receiver=receiver,
                        anonymous_message_id=event.message.id,
                    )
                    self._persist()
        if already_saved:
            await event.reply(f"{handle} is already saved as a backup. Thank you.")
            return
        if existing is not None:
            await event.reply(
                f"{handle} has already been submitted. Transfer it to "
                f"{self._receiver_display_name(existing.receiver)} to complete the donation.")
            return
        if in_use:
            await event.reply(
                f"{handle} is already in use or being processed, so it cannot be added as a spare.")
            return
        recipient = self._receiver_display_name(receiver)
        if receiver == self.config.main.username.casefold():
            instruction = f"Transfer {handle} to {recipient}. Once received, it will be saved as a spare."
        else:
            instruction = (f"Transfer {handle} to {recipient}. The service will then transfer it "
                           "to the main account and save it as a spare.")
        await event.reply(instruction)
        LOG.info("Accepted anonymous backup submission %s for %s via receiver %s",
                 handle, bot.friendly_name, recipient)

    async def _on_pool_ownership(self, receiver: str, message: object) -> None:
        handles = message_handles(message)
        async with self.state_lock:
            matches = [(item, self._matching_candidate(item, handles))
                       for item in self.state.recoveries.values()
                       if item.receiver.casefold() == receiver and item.stage == "waiting_pool"]
            matches = [(item, candidate) for item, candidate in matches if candidate]
            if len(matches) == 1:
                recovery, candidate = matches[0]
                recovery.new_handle = candidate
                recovery.anonymous_message_id = recovery.candidate_message_ids.get(
                    handle_key(candidate), recovery.anonymous_message_id)
                recovery.stage = "transferring"
                self._persist()
                donation = None
            elif not matches:
                donations = [item for key, item in self.state.backup_donations.items()
                             if item.receiver.casefold() == receiver
                             and item.stage == "waiting_receiver"
                             and key in {handle_key(value) for value in handles}]
                recovery = None
                donation = donations[0] if len(donations) == 1 else None
                if donation is not None:
                    donation.stage = "transferring_main"
                    self._persist()
            else:
                recovery = None
                donation = None
        if recovery is None:
            if donation is not None:
                await self._transfer_backup_donation_to_main(donation)
            return
        try:
            await self._transfer_to_main(recovery, receiver)
        except BotFatherRateLimited:
            LOG.warning("Transfer of %s is paused until the BotFather cooldown ends",
                        recovery.new_handle)
        except Exception:
            LOG.exception("Could not transfer %s from pool %s to main",
                          recovery.new_handle, receiver)

    async def _on_main_ownership(self, message: object) -> None:
        handles = message_handles(message)
        async with self.state_lock:
            matches = [(item, self._matching_candidate(item, handles))
                       for item in self.state.recoveries.values()
                       if item.stage in {"transferring", "waiting_main"}]
            matches = [(item, candidate) for item, candidate in matches if candidate]
            if len(matches) == 1:
                recovery, candidate = matches[0]
                recovery.new_handle = candidate
                recovery.anonymous_message_id = recovery.candidate_message_ids.get(
                    handle_key(candidate), recovery.anonymous_message_id)
                recovery.stage = "finalizing"
                self._persist()
                donation = None
            elif not matches:
                donations = [item for key, item in self.state.backup_donations.items()
                             if item.stage in {"waiting_receiver", "transferring_main"}
                             and key in {handle_key(value) for value in handles}]
                recovery = None
                donation = donations[0] if len(donations) == 1 else None
            else:
                recovery = None
                donation = None
        if recovery is not None:
            await self._complete_recovery(recovery)
        elif donation is not None:
            await self._complete_backup_donation(donation)

    def _donation_lock(self, key: str) -> asyncio.Lock:
        return self.donation_locks.setdefault(key.casefold(), asyncio.Lock())

    async def _complete_backup_donation(
            self, donation: BackupDonation, *, verify_main_ownership: bool = False) -> bool:
        handle = donation.handle
        main_key = self.config.main.username.casefold()
        if verify_main_ownership and not await self._account_owns_bot(main_key, handle):
            return False
        bot = self.config.mapping_for_key(donation.key)
        donation_key = handle_key(handle)
        async with self.backup_locks[donation.key]:
            async with self.state_lock:
                if self.state.backup_donations.get(donation_key) is not donation:
                    return False
                backups = self.state.backups.setdefault(donation.key, [])
                if all(handle_key(item) != donation_key for item in backups):
                    backups.append(handle)
                self.state.backup_donations.pop(donation_key, None)
                self._persist()
        LOG.info("Saved donated bot %s as a main-account backup for %s",
                 handle, bot.friendly_name)
        try:
            await self._client_for(donation.receiver).send_message(
                ANONYMOUS_BOT,
                f"Thank you. {handle} is now saved as a spare bot for {bot.friendly_name}.",
                reply_to=donation.anonymous_message_id,
            )
        except Exception as exc:
            LOG.warning("Could not send backup donation confirmation for %s: %s", handle, exc)
        return True

    async def _defer_donation_retry(self, donation: BackupDonation,
                                    retry_at: float | None = None) -> None:
        retry_at = retry_at or (time.time() + BOTFATHER_CHAT_COOLDOWN)
        async with self.state_lock:
            if self.state.backup_donations.get(handle_key(donation.handle)) is donation:
                donation.stage = "transferring_main"
                donation.botfather_retry_at = retry_at
                self._persist()
        LOG.warning("BotFather retry for donated bot %s scheduled until %s UTC",
                    donation.handle, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(retry_at)))

    async def _transfer_backup_donation_to_main(self, donation: BackupDonation) -> None:
        donation_key = handle_key(donation.handle)
        async with self._donation_lock(donation_key):
            async with self.state_lock:
                if (self.state.backup_donations.get(donation_key) is not donation
                        or donation.stage != "transferring_main"):
                    return
            main_key = self.config.main.username.casefold()
            if await self._account_owns_bot(main_key, donation.handle):
                await self._complete_backup_donation(donation)
                return
            receiver = donation.receiver.casefold()
            if receiver == main_key:
                return
            try:
                (method, allow_chat_fallback, allow_mini_app_fallback,
                 retry_at, fallback_retry_at) = (
                    self._botfather_method_plan(receiver, "transfer"))
                if method is None:
                    raise BotFatherRateLimited(
                        "Both BotFather transfer methods are cooling down.", retry_at,
                        method="cooldown", operation="transfer")
                await interact_with_bot(self.main_client, donation.handle)

                def record_transfer_limit(limited_method: str) -> None:
                    self._record_botfather_rate_limit(
                        receiver, "transfer", limited_method)

                async with self.botfather_locks[receiver]:
                    await self.botfather[receiver].transfer(
                        donation.handle, self.config.main.username,
                        force_chat=method == "chat",
                        allow_chat_fallback=allow_chat_fallback,
                        allow_mini_app_fallback=allow_mini_app_fallback,
                        fallback_retry_at=fallback_retry_at,
                        on_rate_limited=record_transfer_limit,
                    )
                await self._complete_backup_donation(
                    donation, verify_main_ownership=True)
            except BotFatherRateLimited as exc:
                if exc.method in {"chat", "mini_app"}:
                    self._record_botfather_rate_limit(
                        receiver, "transfer", exc.method)
                retry_at = self._next_botfather_retry(
                    receiver, "transfer", exc.retry_at)
                await self._defer_donation_retry(donation, retry_at)
                LOG.warning("BotFather rate limited transfer of donated bot %s", donation.handle)
            except Exception:
                LOG.exception("Could not transfer donated bot %s from %s to main",
                              donation.handle, receiver)

    async def _resume_backup_donations(self) -> None:
        for donation in list(self.state.backup_donations.values()):
            try:
                if await self._account_owns_bot(
                        self.config.main.username.casefold(), donation.handle):
                    await self._complete_backup_donation(donation)
                    continue
                if donation.botfather_retry_at > time.time():
                    LOG.info("Donation transfer for %s is paused until its BotFather cooldown ends",
                             donation.handle)
                    continue
                receiver = donation.receiver.casefold()
                if receiver == self.config.main.username.casefold():
                    continue
                if donation.stage == "transferring_main":
                    if await self._account_owns_bot(receiver, donation.handle):
                        await self._transfer_backup_donation_to_main(donation)
                elif await self._account_owns_bot(receiver, donation.handle):
                    async with self.state_lock:
                        if self.state.backup_donations.get(handle_key(donation.handle)) is donation:
                            donation.stage = "transferring_main"
                            self._persist()
                    await self._transfer_backup_donation_to_main(donation)
            except BotFatherRateLimited:
                LOG.warning("Donation transfer of %s is paused by BotFather's rate limit",
                            donation.handle)
            except Exception:
                LOG.exception("Could not resume backup donation %s", donation.handle)

    async def _complete_recovery(self, recovery: Recovery) -> None:
        try:
            await self._finalize(recovery, configure_dialog=True)
            if self.config.automatic_enabled and self.config.prepare_bots:
                await self._safe_ensure_backup(self.config.mapping_for_key(recovery.key), recovery)
        except Exception:
            LOG.exception("Recovery finalization failed for %s", recovery.new_handle)
            async with self.state_lock:
                if self.state.recoveries.get(recovery.key) is recovery:
                    self._persist()

    def _recovery_lock(self, key: str) -> asyncio.Lock:
        return self.recovery_locks.setdefault(key.casefold(), asyncio.Lock())

    @staticmethod
    def _botfather_cooldown_key(account_key: str, operation: str,
                                method: str) -> str:
        return f"{account_key.casefold()}|{operation}|{method}"

    def _botfather_cooldown_until(self, account_key: str, operation: str,
                                  method: str) -> float:
        key = self._botfather_cooldown_key(account_key, operation, method)
        return self.state.botfather_cooldowns.get(key, 0.0)

    def _record_botfather_rate_limit(self, account_key: str, operation: str,
                                    method: str) -> float:
        key = self._botfather_cooldown_key(account_key, operation, method)
        existing = self.state.botfather_cooldowns.get(key, 0.0)
        if existing > time.time():
            return existing
        cooldown = (BOTFATHER_MINI_APP_COOLDOWN if method == "mini_app"
                    else BOTFATHER_CHAT_COOLDOWN)
        retry_at = time.time() + cooldown
        self.state.botfather_cooldowns[key] = retry_at
        self._persist()
        LOG.warning("BotFather %s via %s for %s is rate limited for %s seconds, until %s UTC",
                    operation, method, account_key,
                    cooldown,
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(retry_at)))
        return self.state.botfather_cooldowns[key]

    def _migrate_legacy_botfather_cooldowns(self) -> None:
        """Migrate old combined timers and shorten old Mini App cooldowns."""
        now = time.time()
        changed = False

        if self.state.botfather_cooldown_version < BOTFATHER_COOLDOWN_STATE_VERSION:
            # Earlier versions saved Mini App rate limits as 24 hours. Their
            # expiry is based on the original attempt, so subtract the 18-hour
            # difference once and keep the cooldown's original start time.
            shorten_by = BOTFATHER_CHAT_COOLDOWN - BOTFATHER_MINI_APP_COOLDOWN
            for key, retry_at in list(self.state.botfather_cooldowns.items()):
                if key.rpartition("|")[2] != "mini_app" or retry_at <= now:
                    continue
                self.state.botfather_cooldowns[key] = max(now, retry_at - shorten_by)
                changed = True
            self.state.botfather_cooldown_version = BOTFATHER_COOLDOWN_STATE_VERSION
            changed = True

        def remember_chat_limit(account_key: str, operation: str,
                                retry_at: float) -> None:
            nonlocal changed
            key = self._botfather_cooldown_key(account_key, operation, "chat")
            if retry_at > self.state.botfather_cooldowns.get(key, 0.0):
                self.state.botfather_cooldowns[key] = retry_at
                changed = True

        creator_key = (self.config.creator.username.casefold()
                       if self.config.creator else None)
        for recovery in self.state.recoveries.values():
            retry_at = recovery.botfather_retry_at
            if retry_at <= now:
                continue
            if recovery.stage == "transferring":
                remember_chat_limit(recovery.receiver, "transfer", retry_at)
            elif recovery.stage == "automatic_preparing" and creator_key:
                operation = ("transfer" if recovery.key.casefold()
                             in self.state.pending_creations else "create")
                remember_chat_limit(creator_key, operation, retry_at)
            else:
                continue
            recovery.botfather_retry_at = 0.0
            changed = True

        for donation in self.state.backup_donations.values():
            retry_at = donation.botfather_retry_at
            if retry_at > now and donation.stage == "transferring_main":
                remember_chat_limit(donation.receiver, "transfer", retry_at)
                donation.botfather_retry_at = 0.0
                changed = True

        if creator_key and self.state.creator_retry_at > now:
            retry_at = self.state.creator_retry_at
            operation = "transfer" if self.state.pending_creations else "create"
            remember_chat_limit(creator_key, operation, retry_at)
            self.state.creator_retry_at = 0.0
            changed = True

        if changed:
            LOG.info("Migrated saved BotFather cooldowns to method-specific state")
            if self.store.path.exists():
                self._persist()

    def _botfather_method_plan(
        self, account_key: str, operation: str,
    ) -> tuple[str | None, bool, bool, float | None, float | None]:
        now = time.time()
        mini_app_until = self._botfather_cooldown_until(
            account_key, operation, "mini_app")
        chat_until = self._botfather_cooldown_until(account_key, operation, "chat")
        mini_app_limited = mini_app_until > now
        chat_limited = chat_until > now
        if mini_app_limited and chat_limited:
            return None, False, False, min(mini_app_until, chat_until), None
        if mini_app_limited:
            return "chat", False, False, None, mini_app_until
        if chat_limited:
            return "mini_app", False, False, None, chat_until
        return "mini_app", True, True, None, None

    def _next_botfather_retry(self, account_key: str, operation: str,
                              fallback: float | None = None) -> float:
        now = time.time()
        active = [self._botfather_cooldown_until(account_key, operation, method)
                  for method in ("mini_app", "chat")]
        active = [value for value in active if value > now]
        return min(active) if active else (fallback or now + BOTFATHER_CHAT_COOLDOWN)

    async def _defer_botfather_retry(self, recovery: Recovery | None,
                                    retry_at: float | None = None,
                                    *, creator_account: bool = False) -> None:
        retry_at = retry_at or (time.time() + BOTFATHER_CHAT_COOLDOWN)
        async with self.state_lock:
            active = recovery is not None and self.state.recoveries.get(recovery.key) is recovery
            if active:
                recovery.botfather_retry_at = retry_at
            if creator_account or not active:
                if (self.state.creator_retry_at <= time.time()
                        or retry_at < self.state.creator_retry_at):
                    self.state.creator_retry_at = retry_at
            self._persist()
        LOG.warning("BotFather cooldown scheduled until %s UTC",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(retry_at)))

    async def _botfather_retry_monitor(self) -> None:
        while True:
            await asyncio.sleep(RETRY_MONITOR_INTERVAL)
            now = time.time()
            async with self.state_lock:
                retry_prepared = 0 < self.state.creator_retry_at <= now
                due = [item for item in self.state.recoveries.values()
                       if 0 < item.botfather_retry_at <= now]
                due_donations = [item for item in self.state.backup_donations.values()
                                 if 0 < item.botfather_retry_at <= now
                                 and item.stage == "transferring_main"]
                if retry_prepared:
                    self.state.creator_retry_at = 0.0
                for recovery in due:
                    recovery.botfather_retry_at = 0.0
                for donation in due_donations:
                    donation.botfather_retry_at = 0.0
                if retry_prepared or due or due_donations:
                    self._persist()
            for recovery in due:
                try:
                    if self.state.recoveries.get(recovery.key) is not recovery:
                        continue
                    if recovery.stage == "automatic_preparing":
                        await self._run_automatic_or_fallback(
                            recovery, self.config.mapping_for_key(recovery.key))
                    elif recovery.stage == "transferring":
                        await self._transfer_to_main(recovery, recovery.receiver.casefold())
                except BotFatherRateLimited:
                    LOG.warning("BotFather cooldown was extended for recovery %s",
                                recovery.key)
                except Exception:
                    LOG.exception("Could not retry BotFather operation for %s", recovery.key)
            for donation in due_donations:
                try:
                    await self._transfer_backup_donation_to_main(donation)
                except Exception:
                    LOG.exception("Could not retry donated bot transfer for %s", donation.handle)
            if retry_prepared:
                try:
                    await self._reconcile_prepared_bots()
                    await self._ensure_prepared_bots()
                except BotFatherRateLimited:
                    LOG.warning("Prepared-bot work is paused until the BotFather cooldown ends")
                except Exception:
                    LOG.exception("Could not retry prepared-bot work after cooldown")

    async def _run_automatic_or_fallback(self, recovery: Recovery,
                                         bot: BotSpec) -> None:
        try:
            await self._run_automatic_recovery(recovery, bot)
        except (AutomaticWaitExceeded, FloodWaitError) as exc:
            if recovery.new_handle is not None:
                raise
            LOG.warning("Automatic recovery for %s is switching to manual: %s",
                        bot.friendly_name, exc)
            async with self.state_lock:
                if self.state.recoveries.get(recovery.key) is not recovery:
                    return
                recovery.stage = "starting"
                recovery.mode = "manual"
                if not recovery.reward_code:
                    recovery.reward_code = reward_code()
                self._persist()
            await self._prepare_manual_recovery(recovery, bot)

    async def _run_automatic_recovery(self, recovery: Recovery, bot: BotSpec) -> None:
        if recovery.new_handle is None:
            async with self.state_lock:
                has_backup = bool(self.state.backups.get(bot.prefix.casefold()))
            if (not has_backup
                    and (not self.creator_client_available or self.creator_client is None)):
                raise AutomaticWaitExceeded("automatic creator account is unavailable")
            await self._claim_backup(bot, recovery)
        if recovery.stage == "automatic_preparing":
            async with self.state_lock:
                if self.state.recoveries.get(recovery.key) is not recovery:
                    return
                recovery.stage = "finalizing"
                self._persist()
        await self._finalize(recovery, configure_dialog=True)
        if self.config.prepare_bots:
            await self._safe_ensure_backup(bot, recovery)

    async def _ensure_prepared_bots(self) -> None:
        if not (self.config.automatic_enabled and self.config.prepare_bots):
            return
        async def prepare(bot: BotSpec) -> None:
            await self._safe_ensure_backup(bot)
        await asyncio.gather(*(prepare(bot) for bot in self.config.bots))

    async def _safe_ensure_backup(self, bot: BotSpec,
                                  recovery: Recovery | None = None) -> None:
        try:
            await self._ensure_backup(bot, recovery)
        except BotFatherRateLimited:
            LOG.warning("Backup preparation for %s is paused by BotFather's rate limit",
                        bot.friendly_name)
        except Exception:
            LOG.exception("Could not prepare a backup for %s", bot.friendly_name)

    async def _reconcile_prepared_bots(self) -> None:
        main_key = self.config.main.username.casefold()
        for bot in self.config.bots:
            key = bot.prefix.casefold()
            async with self.backup_locks[key]:
                async with self.state_lock:
                    pending = self.state.pending_creations.get(key)
                    backups = self.state.backups.get(key, [])
                    queued_pending = bool(pending and any(
                        handle_key(item) == handle_key(pending) for item in backups))
                    if queued_pending:
                        self.state.pending_creations.pop(key, None)
                        self.state.pending_creation_chat_modes.pop(key, None)
                        self._persist()
                if not pending:
                    continue
                if queued_pending:
                    LOG.info("Pending bot %s is already in the backup queue; clearing duplicate pending state",
                             pending)
                    continue
                if await self._account_owns_bot(main_key, pending):
                    async with self.state_lock:
                        if self.state.pending_creations.get(key) == pending:
                            self.state.pending_creations.pop(key, None)
                            self.state.pending_creation_chat_modes.pop(key, None)
                            saved = self.state.backups.setdefault(key, [])
                            if all(handle_key(item) != handle_key(pending) for item in saved):
                                saved.append(pending)
                            self._persist()
                    continue
                # A saved spare already satisfies preparation. Leave an
                # interrupted creator-owned handle untouched until the queue
                # is empty, rather than issuing a redundant transfer request.
                if backups:
                    LOG.info("Keeping pending bot %s with the creator while %s saved spare(s) remain",
                             pending, len(backups))
                    continue
                if (self.config.automatic_enabled and self.config.prepare_bots
                        and self.state.creator_retry_at <= time.time()):
                    await self._safe_create_backup(bot)

    async def _safe_create_backup(self, bot: BotSpec) -> None:
        try:
            await self._create_backup(bot)
        except BotFatherRateLimited:
            LOG.warning("Saved backup preparation for %s is paused by BotFather's rate limit",
                        bot.friendly_name)
        except Exception:
            LOG.exception("Could not verify or finish transferring a saved backup for %s",
                          bot.friendly_name)

    async def _ensure_backup(self, bot: BotSpec,
                             recovery: Recovery | None = None) -> None:
        key = bot.prefix.casefold()
        async with self.backup_locks[key]:
            async with self.state_lock:
                saved_count = len(self.state.backups.get(key, []))
            if saved_count:
                LOG.info("Keeping %s saved spare(s) for %s; no creator transfer is needed",
                         saved_count, bot.friendly_name)
                return
            await self._create_backup(bot, recovery)

    async def _claim_backup(self, bot: BotSpec, recovery: Recovery) -> str:
        key = bot.prefix.casefold()
        async with self.backup_locks[key]:
            async with self.state_lock:
                backups = self.state.backups.get(key, [])
                if backups:
                    handle = backups.pop(0)
                    if not backups:
                        self.state.backups.pop(key, None)
                    recovery.new_handle = handle
                    recovery.candidate_handles = [handle]
                    self._persist()
                    return handle
            handle = await self._create_backup(bot, recovery)
            async with self.state_lock:
                backups = self.state.backups.get(key, [])
                if handle in backups:
                    backups.remove(handle)
                    if not backups:
                        self.state.backups.pop(key, None)
                recovery.new_handle = handle
                recovery.candidate_handles = [handle]
                self._persist()
            return handle

    async def _create_backup(self, bot: BotSpec,
                             recovery: Recovery | None = None) -> str:
        key = bot.prefix.casefold()
        async with self.state_lock:
            backups = self.state.backups.get(key, [])
            if backups:
                # A queue may have been populated while this operation waited
                # for a lock or resumed after an older interrupted run.
                return backups[0]
        creator = self.config.creator
        creator_client = self.creator_client
        if creator is None or creator_client is None or not self.creator_client_available:
            raise RecoveryError("Automatic recovery creator account is not configured or online.")
        creator_key = creator.username.casefold()
        async with self.botfather_locks[creator_key]:
            async with self.state_lock:
                handle = self.state.pending_creations.get(key)
                wait = self.state.creator_next_at - time.time()
                operation_recovery = (
                    recovery if recovery is not None
                    and self.state.recoveries.get(recovery.key) is recovery
                    else None
                )
            already_owned = bool(
                handle and await self._account_owns_bot(
                    self.config.main.username.casefold(), handle))
            if already_owned:
                LOG.info("Pending bot %s is already owned by main; skipping ownership transfer",
                         handle)
                wait = 0.0
            if wait > AUTOMATIC_WAIT_LIMIT:
                raise AutomaticWaitExceeded(
                    f"creator account is cooling down for another {wait:.0f} seconds")
            if wait > 0:
                LOG.info("Waiting %.0f seconds before the next BotFather create operation", wait)
                await asyncio.sleep(wait)
            creator_botfather = self.botfather[creator_key]

            def record_rate_limit(operation: str, method: str) -> None:
                self._record_botfather_rate_limit(creator_key, operation, method)

            try:
                if not already_owned:
                    if handle is None:
                        (create_method, allow_chat_fallback, _, create_retry_at,
                         create_fallback_retry_at) = (
                            self._botfather_method_plan(creator_key, "create"))
                        if create_method is None:
                            raise BotFatherRateLimited(
                                "Both BotFather creation methods are cooling down.",
                                create_retry_at, method="cooldown", operation="create")
                        handle = await creator_botfather.create_bot(
                            bot.friendly_name, bot.prefix,
                            force_chat=create_method == "chat",
                            allow_chat_fallback=allow_chat_fallback,
                            fallback_retry_at=create_fallback_retry_at,
                            on_rate_limited=lambda method: record_rate_limit(
                                "create", method),
                        )
                        async with self.state_lock:
                            self.state.pending_creations[key] = handle
                            self.state.creator_next_at = time.time() + CREATE_INTERVAL
                            self._persist()
                        LOG.info("Created backup bot %s; ownership transfer is pending", handle)
                    try:
                        (transfer_method, allow_chat_fallback, allow_mini_app_fallback,
                         transfer_retry_at, transfer_fallback_retry_at) = (
                            self._botfather_method_plan(creator_key, "transfer"))
                        if transfer_method is None:
                            raise BotFatherRateLimited(
                                "Both BotFather transfer methods are cooling down.",
                                transfer_retry_at, method="cooldown", operation="transfer")
                        await interact_with_bot(self.main_client, handle)
                        await creator_botfather.transfer(
                            handle, self.config.main.username,
                            recovery=operation_recovery,
                            force_chat=transfer_method == "chat",
                            allow_chat_fallback=allow_chat_fallback,
                            allow_mini_app_fallback=allow_mini_app_fallback,
                            fallback_retry_at=transfer_fallback_retry_at,
                            on_rate_limited=lambda method: record_rate_limit(
                                "transfer", method),
                        )
                    except Exception:
                        # The transfer may have completed before the process lost
                        # its confirmation. BotFather's owner list is authoritative.
                        if not await self._account_owns_bot(
                                self.config.main.username.casefold(), handle):
                            raise
            except BotFatherRateLimited as exc:
                operation = exc.operation or ("create" if handle is None else "transfer")
                if exc.method in {"chat", "mini_app"}:
                    record_rate_limit(operation, exc.method)
                retry_at = self._next_botfather_retry(
                    creator_key, operation, exc.retry_at)
                await self._defer_botfather_retry(
                    operation_recovery, retry_at, creator_account=True)
                raise
            except FloodWaitError as exc:
                async with self.state_lock:
                    self.state.creator_next_at = max(
                        self.state.creator_next_at, time.time() + exc.seconds)
                    self._persist()
                raise
            except Exception:
                async with self.state_lock:
                    self._persist()
                raise
        async with self.state_lock:
            self.state.pending_creations.pop(key, None)
            self.state.pending_creation_chat_modes.pop(key, None)
            backups = self.state.backups.setdefault(key, [])
            if all(handle_key(item) != handle_key(handle) for item in backups):
                backups.append(handle)
            self._persist()
        LOG.info("Prepared backup %s for %s", handle, bot.friendly_name)
        return handle

    async def _account_owns_bot(self, account_key: str, handle: str) -> bool:
        botfather = self.botfather.get(account_key)
        if botfather is None:
            return False
        async with self.botfather_locks[account_key]:
            try:
                return handle_key(handle) in await botfather.owned_handles()
            except (asyncio.TimeoutError, OSError, RPCError, ValueError, RecoveryError):
                return False

    async def _transfer_to_main(self, recovery: Recovery, pool: str) -> None:
        already_owned = False
        async with self._recovery_lock(recovery.key):
            if not recovery.new_handle:
                raise RecoveryError("Cannot transfer ownership without a replacement handle.")
            main_key = self.config.main.username.casefold()
            if await self._account_owns_bot(main_key, recovery.new_handle):
                async with self.state_lock:
                    if self.state.recoveries.get(recovery.key) is recovery:
                        recovery.stage = "finalizing"
                        self._persist()
                already_owned = True
            else:
                (method, allow_chat_fallback, allow_mini_app_fallback,
                 retry_at, fallback_retry_at) = (
                    self._botfather_method_plan(pool, "transfer"))
                if method is None:
                    await self._defer_botfather_retry(recovery, retry_at)
                    raise BotFatherRateLimited(
                        "Both BotFather transfer methods are cooling down.", retry_at,
                        method="cooldown", operation="transfer")
                await interact_with_bot(self.main_client, recovery.new_handle)

                def record_transfer_limit(limited_method: str) -> None:
                    self._record_botfather_rate_limit(
                        pool, "transfer", limited_method)

                try:
                    async with self.botfather_locks[pool]:
                        await self.botfather[pool].transfer(
                            recovery.new_handle, self.config.main.username,
                            force_chat=method == "chat",
                            allow_chat_fallback=allow_chat_fallback,
                            allow_mini_app_fallback=allow_mini_app_fallback,
                            fallback_retry_at=fallback_retry_at,
                            on_rate_limited=record_transfer_limit,
                        )
                except BotFatherRateLimited as exc:
                    if exc.method in {"chat", "mini_app"}:
                        self._record_botfather_rate_limit(
                            pool, "transfer", exc.method)
                    retry_at = self._next_botfather_retry(
                        pool, "transfer", exc.retry_at)
                    await self._defer_botfather_retry(recovery, retry_at)
                    raise
                except Exception:
                    async with self.state_lock:
                        self._persist()
                    raise
                async with self.state_lock:
                    if (self.state.recoveries.get(recovery.key) is recovery
                            and recovery.stage == "transferring"):
                        recovery.stage = "waiting_main"
                    self._persist()
                LOG.info("Transferred %s from %s to main", recovery.new_handle, pool)
        if already_owned:
            await self._complete_recovery(recovery)

    async def _configure_recovered_dialog(self, recovery: Recovery) -> None:
        if not recovery.new_handle:
            return
        try:
            old_peer = await self.main_client.get_input_entity(recovery.old_handle)
        except (OSError, RPCError, ValueError):
            old_peer = None
            try:
                old_key = handle_key(recovery.old_handle)
                for dialog in await self.main_client.get_dialogs():
                    entity = getattr(dialog, "entity", None)
                    if handle_key(str(getattr(entity, "username", "") or "")) == old_key:
                        old_peer = await self.main_client.get_input_entity(entity)
                        break
            except (OSError, RPCError, ValueError):
                pass
        if old_peer is not None:
            try:
                await self.main_client(functions.messages.ToggleDialogPinRequest(
                    peer=old_peer, pinned=False))
            except (OSError, RPCError, ValueError) as exc:
                LOG.warning("Could not unpin old bot %s: %s", recovery.old_handle, exc)
        try:
            new_peer = await self.main_client.get_input_entity(recovery.new_handle)
            await self.main_client(functions.account.UpdateNotifySettingsRequest(
                peer=types.InputNotifyPeer(peer=new_peer),
                settings=types.InputPeerNotifySettings(mute_until=2147483647)))
            await self.main_client(functions.messages.ToggleDialogPinRequest(
                peer=new_peer, pinned=True))
        except (OSError, RPCError, ValueError) as exc:
            LOG.warning("Could not mute/pin recovered bot chat %s: %s",
                        recovery.new_handle, exc)

    async def _finalize(self, recovery: Recovery, *, configure_dialog: bool = False) -> None:
        async with self._recovery_lock(recovery.key):
            async with self.state_lock:
                if self.state.recoveries.get(recovery.key) is not recovery:
                    return
            if configure_dialog:
                await self._configure_recovered_dialog(recovery)
            await self._finalize_steps(recovery)

    async def _finalize_steps(self, recovery: Recovery) -> None:
        bot = self.config.mapping_for_key(recovery.key)
        if not recovery.new_handle:
            raise RecoveryError("Cannot finalize a recovery without a replacement bot.")

        async def checkpoint(stage: str) -> None:
            async with self.state_lock:
                if self.state.recoveries.get(recovery.key) is recovery:
                    recovery.stage = stage
                    self._persist()

        if recovery.stage in {"waiting_main", "finalizing"}:
            await checkpoint("finalizing_restore")

        if recovery.stage == "finalizing_restore":
            main_key = self.config.main.username.casefold()
            async with self.botfather_locks[main_key]:
                token = await restore_snapshot(
                    self.main_client, self.botfather[main_key],
                    self.config, bot, recovery.new_handle)
            write_yaml_path(bot.config_path, bot.token_path, token)
            write_yaml_path(bot.config_path, "recovery.reward_code", recovery.reward_code)
            try:
                subprocess.run(bot.restart_command, check=True, timeout=60,
                               capture_output=True, text=True)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RecoveryError(
                    f"Restart command failed ({' '.join(bot.restart_command)}): {exc}") from exc
            await asyncio.sleep(10)
            await checkpoint("finalizing_about")

        if recovery.stage == "finalizing_about":
            handles = [
                recovery.new_handle
                if configured.prefix.casefold() == bot.prefix.casefold()
                else configured.handle
                for configured in self.config.bots
            ]
            targets = [handle for handle in handles if handle]
            main_key = self.config.main.username.casefold()
            async with self.botfather_locks[main_key]:
                targets = await update_bot_profile_references(
                    self.main_client, self.botfather[main_key], targets,
                    bot.prefix, recovery.new_handle)
            for handle in targets:
                await update_runtime_about(
                    self.main_client, handle, bot.prefix, recovery.new_handle)
            await checkpoint("finalizing_config")

        if recovery.stage == "finalizing_config":
            async with self.config_write_lock:
                self.config.set_current_handle(bot, recovery.new_handle)
            await checkpoint("finalizing_descriptions")

        if recovery.stage in {"finalizing_descriptions", "finalizing_publish"}:
            async with self.description_lock:
                await update_chat_description(
                    self.main_client, self.config.announcement_channel,
                    bot.prefix, recovery.new_handle)
                await update_chat_description(
                    self.main_client, self.config.announcement_group,
                    bot.prefix, recovery.new_handle)
            await checkpoint("finalizing_publish")

        if recovery.stage == "finalizing_publish":
            async with self.publish_lock:
                await publish_page(self.config, bot, recovery.new_handle)
            await checkpoint("finalizing_announce")

        if recovery.stage == "finalizing_announce":
            text = render(
                self.config.restored_template,
                friendly_name=bot.friendly_name,
                new_handle=recovery.new_handle,
                old_handle=recovery.old_handle,
            )
            text = bot_handle_replacer(bot.prefix, recovery.new_handle, text)
            existing = await self.main_client.get_messages(
                self.config.announcement_channel, limit=50)
            prior = next((item for item in existing if response_text(item) == text), None)
            if prior is None:
                posted = await self.main_client.send_message(
                    self.config.announcement_channel, text)
                LOG.info("Posted restoration announcement for %s to %s (message %s)",
                         bot.friendly_name, self.config.announcement_channel,
                         getattr(posted, "id", "unknown"))
            else:
                LOG.info("Skipped duplicate restoration announcement for %s; "
                         "matching message %s is already in %s",
                         bot.friendly_name, getattr(prior, "id", "unknown"),
                         self.config.announcement_channel)
            donation_link = ""
            if self.config.retain_anonymous_link:
                donation_link = await self._get_or_create_anonymous_link(
                    recovery.receiver.casefold(), recovery.anonymous_link)
                async with self.state_lock:
                    if self.state.recoveries.get(recovery.key) is recovery:
                        recovery.anonymous_link = donation_link
                        self._persist()
            if donation_link:
                donation_text = render(
                    self.config.donation_template,
                    friendly_name=bot.friendly_name,
                    new_handle=recovery.new_handle,
                    old_handle=recovery.old_handle,
                    handle_prefix=bot.prefix,
                    anonymous_link=donation_link,
                )
                existing = await self.main_client.get_messages(
                    self.config.announcement_channel, limit=50)
                prior = next((item for item in existing
                              if response_text(item) == donation_text), None)
                if prior is None:
                    await self.main_client.send_message(
                        self.config.announcement_channel, donation_text)
            await checkpoint("finalizing_cleanup")

        if recovery.stage == "finalizing_cleanup":
            if recovery.anonymous_message_id is not None:
                client = self._client_for(recovery.receiver.casefold())
                message = (
                    f"Thank you. {bot.friendly_name} has been restored as {recovery.new_handle}. "
                    "As thanks, you will receive 100 credits after sending this code in the "
                    f"restored bot: {recovery.reward_code}."
                )
                replies = await client.get_messages(ANONYMOUS_BOT, limit=30)
                exists = any(
                    response_text(item) == message
                    and getattr(getattr(item, "reply_to", None), "reply_to_msg_id", None)
                    == recovery.anonymous_message_id
                    for item in replies
                )
                if not exists:
                    await client.send_message(
                        ANONYMOUS_BOT, message, reply_to=recovery.anonymous_message_id)
            await checkpoint("finalizing_delete_announcement")

        if recovery.stage == "finalizing_delete_announcement":
            if recovery.announcement_message_id is not None:
                try:
                    await self.main_client.delete_messages(
                        self.config.announcement_channel, recovery.announcement_message_id)
                except MessageIdInvalidError:
                    pass
            await checkpoint("finalizing_revoke")

        if recovery.stage == "finalizing_revoke":
            receiver = recovery.receiver.casefold()
            async with self.anonymous_locks[receiver]:
                async with self.state_lock:
                    another = any(item.key != recovery.key
                                  and item.receiver.casefold() == receiver
                                  for item in self.state.recoveries.values())
                if recovery.anonymous_link and not another:
                    if self.config.retain_anonymous_link:
                        async with self.state_lock:
                            self.state.anonymous_links[receiver] = recovery.anonymous_link
                            self._persist()
                        LOG.info("Keeping the anonymous submission link active for %s", receiver)
                    else:
                        await revoke_anonymous_link(self._client_for(receiver))
                async with self.state_lock:
                    if recovery.anonymous_link and not another \
                            and not self.config.retain_anonymous_link:
                        self.state.anonymous_links.pop(receiver, None)
                    self.state.recoveries.pop(recovery.key, None)
                    self._persist()
            LOG.info("Recovery complete for %s", bot.friendly_name)

    async def _resume_recoveries(self) -> None:
        resumable = {
            "starting", "automatic_preparing", "transferring", "finalizing",
            "finalizing_restore", "finalizing_about", "finalizing_config",
            "finalizing_descriptions", "finalizing_publish", "finalizing_announce",
            "finalizing_cleanup", "finalizing_delete_announcement", "finalizing_revoke",
        }

        async def resume(recovery: Recovery) -> None:
            if recovery.stage == "waiting_main":
                if (recovery.new_handle
                        and await self._account_owns_bot(
                            self.config.main.username.casefold(), recovery.new_handle)):
                    async with self.state_lock:
                        if self.state.recoveries.get(recovery.key) is recovery:
                            recovery.stage = "finalizing"
                            self._persist()
                    await self._complete_recovery(recovery)
                return
            if recovery.stage not in resumable:
                return
            if recovery.botfather_retry_at > time.time():
                LOG.info("Recovery for %s is paused by BotFather cooldown",
                         recovery.old_handle)
                return
            if recovery.botfather_retry_at:
                async with self.state_lock:
                    if self.state.recoveries.get(recovery.key) is recovery:
                        recovery.botfather_retry_at = 0.0
                        self._persist()
            try:
                if recovery.stage == "starting":
                    await self._prepare_manual_recovery(
                        recovery, self.config.mapping_for_key(recovery.key))
                elif recovery.stage == "automatic_preparing":
                    await self._run_automatic_or_fallback(
                        recovery, self.config.mapping_for_key(recovery.key))
                elif recovery.stage == "transferring":
                    await self._transfer_to_main(recovery, recovery.receiver.casefold())
                elif recovery.mode == "automatic" and recovery.stage == "finalizing":
                    await self._run_automatic_recovery(
                        recovery, self.config.mapping_for_key(recovery.key))
                else:
                    await self._finalize(recovery, configure_dialog=True)
                    if self.config.automatic_enabled and self.config.prepare_bots:
                        await self._safe_ensure_backup(
                            self.config.mapping_for_key(recovery.key), recovery)
            except BotFatherRateLimited:
                LOG.warning("Recovery for %s remains paused by BotFather's rate limit",
                            recovery.new_handle or recovery.old_handle)
            except Exception:
                LOG.exception("Could not resume recovery for %s at stage %s",
                              recovery.new_handle or recovery.old_handle, recovery.stage)
                async with self.state_lock:
                    if self.state.recoveries.get(recovery.key) is recovery:
                        self._persist()

        await asyncio.gather(*(resume(item) for item in list(self.state.recoveries.values())))


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("The recovery configuration must be a YAML mapping.")
    return Config(raw, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check-config", action="store_true",
                        help="validate configuration and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"Configuration is valid: {args.config}")
            return 0
        asyncio.run(RecoveryService(config).run())
        return 0
    except (RecoveryError, RPCError, ValueError, OSError) as exc:
        LOG.error("Recovery service stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
