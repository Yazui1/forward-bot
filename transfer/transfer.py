from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path

from telethon import TelegramClient, functions, types
from telethon.errors import RPCError
from telethon.sessions import StringSession
import yaml


HERE = Path(__file__).resolve().parent
BOTFATHER = "BotFather"
SETTINGS_PATH = HERE / "config.yml"
DEFAULT_SESSION = HERE / ".session" / "transfer"


def username(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("A bot username is required.")
    return "@" + value.lstrip("@")


def load_settings() -> dict[str, object]:
    if SETTINGS_PATH.exists():
        try:
            settings = yaml.safe_load(
                SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            raise ValueError(f"Could not read {SETTINGS_PATH}: {exc}") from exc
        if not isinstance(settings, dict):
            raise ValueError(f"{SETTINGS_PATH} must contain a YAML mapping.")
    else:
        print("First run: Telegram API credentials will be saved locally.")
        api_id = os.environ.get("TELEGRAM_API_ID", "").strip() or input(
            "Telegram API ID: "
        ).strip()
        api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip() or getpass.getpass(
            "Telegram API hash: "
        ).strip()
        if not api_id or not api_hash:
            raise ValueError("Both Telegram API ID and API hash are required.")
        settings = {
            "api_id": api_id,
            "api_hash": api_hash,
            "session": ".session/transfer",
        }
        SETTINGS_PATH.write_text(
            yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
        )
        print(f"Saved settings to {SETTINGS_PATH}")

    if not str(settings.get("api_id", "")).strip():
        raise ValueError(f"api_id is missing from {SETTINGS_PATH}.")
    if not str(settings.get("api_hash", "")).strip():
        raise ValueError(f"api_hash is missing from {SETTINGS_PATH}.")
    return settings


def response_text(message: object) -> str:
    return str(getattr(message, "raw_text", "") or "")


def replace_bot_usernames(value: str, mappings: list[tuple[str, str]]) -> str:
    replacements = {source.casefold(): target for source, target in mappings}
    pattern = "|".join(
        re.escape(source)
        for source, _ in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True)
    )
    return re.sub(
        pattern,
        lambda match: replacements[match.group(0).casefold()],
        value,
        flags=re.IGNORECASE,
    )


def parse_mapping(value: str) -> tuple[str, str]:
    if value.count(":") != 1:
        raise ValueError(
            f"Invalid mapping {value!r}; expected exactly @old_bot:@new_bot."
        )
    source, target = (username(part) for part in value.split(":", 1))
    if source.casefold() == target.casefold():
        raise ValueError(f"Source and target must differ in mapping {value!r}.")
    return source, target


def ensure_botfather_ok(message: object, action: str) -> None:
    text = response_text(message)
    lowered = text.lower()
    failures = ("error", "invalid", "sorry", "failed", "not enough rights")
    if any(word in lowered for word in failures):
        raise RuntimeError(f"BotFather rejected {action}: {text}")


async def get_source_commands(
    client: TelegramClient,
    source_username: str,
    api_id: int,
    api_hash: str,
) -> list[types.BotCommand]:
    async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/token")
        ensure_botfather_ok(await conv.get_response(), "requesting the source token")
        await conv.send_message(source_username)
        token_message = await conv.get_response()
        ensure_botfather_ok(token_message, "reading the source token")

    match = re.search(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b",
                      response_text(token_message))
    if not match:
        raise RuntimeError(
            "Could not find the source bot token in BotFather's response: "
            + response_text(token_message)
        )

    bot_client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await bot_client.start(bot_token=match.group(0))
        result = await bot_client(
            functions.bots.GetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code="",
            )
        )
        return list(result)
    finally:
        await bot_client.disconnect()


async def copy_commands(
    client: TelegramClient,
    target_username: str,
    commands: list[types.BotCommand],
) -> None:
    payload = "\n".join(
        f"{item.command} - {item.description}" for item in commands)
    if not payload:
        payload = "/empty"

    async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/setcommands")
        ensure_botfather_ok(await conv.get_response(), "starting command transfer")
        await conv.send_message(target_username)
        ensure_botfather_ok(await conv.get_response(), "selecting the target bot")
        await conv.send_message(payload)
        final = await conv.get_response()
        ensure_botfather_ok(final, "setting commands")
        if not any(word in response_text(final).lower() for word in ("success", "updated", "done")):
            raise RuntimeError(
                "BotFather returned an unexpected command response: "
                + response_text(final)
            )


async def regenerate_token(client: TelegramClient, target_username: str) -> None:
    async with client.conversation(BOTFATHER, timeout=45, exclusive=True) as conv:
        await conv.send_message("/cancel")
        await conv.get_response()
        await conv.send_message("/revoke")
        ensure_botfather_ok(await conv.get_response(), "starting token regeneration")
        await conv.send_message(target_username)
        token_message = await conv.get_response()
        ensure_botfather_ok(token_message, "regenerating the target token")

    if not re.search(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", response_text(token_message)):
        raise RuntimeError(
            "BotFather did not return a new token after /revoke: "
            + response_text(token_message)
        )
    print("  regenerated token; the new token is in the BotFather chat")


async def transfer(args: argparse.Namespace) -> None:
    settings = load_settings()
    api_id = int(str(settings["api_id"]))
    api_hash = str(settings["api_hash"])
    mapping_values = [value for value in args.items if ":" in value]
    description_targets = [value for value in args.items if ":" not in value]
    if not mapping_values:
        raise ValueError("At least one @old_bot:@new_bot mapping is required.")
    mappings = [parse_mapping(value) for value in mapping_values]
    source_names = [source.casefold() for source, _ in mappings]
    target_names = [target.casefold() for _, target in mappings]
    if len(source_names) != len(set(source_names)):
        raise ValueError("Each source bot may appear only once.")
    if len(target_names) != len(set(target_names)):
        raise ValueError("Each target bot may appear only once.")

    session = Path(str(settings.get("session")
                   or DEFAULT_SESSION)).expanduser()
    if not session.is_absolute():
        session = HERE / session
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), api_id, api_hash)

    async with client:
        me = await client.get_me()
        print(
            f"Logged in as @{me.username}" if me.username else f"Logged in as user {me.id}")

        for source_name, target_name in mappings:
            await transfer_one(
                client, api_id, api_hash, source_name, target_name, mappings
            )

        for chat_name in description_targets:
            await rewrite_chat_description(client, chat_name, mappings)

    print(
        f"Transfer complete: {len(mappings)} bot(s), "
        f"{len(description_targets)} chat description(s)."
    )


async def rewrite_chat_description(
    client: TelegramClient,
    chat_name: str,
    mappings: list[tuple[str, str]],
) -> None:
    peer = await client.get_input_entity(chat_name)
    entity = await client.get_entity(peer)
    if isinstance(entity, types.Channel):
        full = await client(functions.channels.GetFullChannelRequest(peer))
    elif isinstance(entity, types.Chat):
        full = await client(functions.messages.GetFullChatRequest(entity.id))
    else:
        raise ValueError(f"Description target {chat_name!r} is not a channel or group.")

    old_description = str(getattr(full.full_chat, "about", "") or "")
    new_description = replace_bot_usernames(old_description, mappings)
    if new_description == old_description:
        print(f"Description {chat_name}: no matching bot usernames")
        return
    await client(functions.messages.EditChatAboutRequest(peer=peer, about=new_description))
    print(f"Description {chat_name}: updated")


async def transfer_one(
    client: TelegramClient,
    api_id: int,
    api_hash: str,
    source_name: str,
    target_name: str,
    mappings: list[tuple[str, str]],
) -> None:
    source = await client.get_input_entity(source_name)
    target = await client.get_input_entity(target_name)
    source_entity = await client.get_entity(source)
    target_entity = await client.get_entity(target)
    if not getattr(source_entity, "bot", False) or not getattr(target_entity, "bot", False):
        raise ValueError("Both usernames in every mapping must identify Telegram bots.")

    info = await client(functions.bots.GetBotInfoRequest(lang_code="", bot=source))
    print(f"Copying {source_name} -> {target_name} ...")

    copied_name = replace_bot_usernames(source_entity.first_name, mappings)
    copied_about = replace_bot_usernames(info.about or "", mappings)
    copied_description = replace_bot_usernames(info.description or "", mappings)

    await client(
        functions.bots.SetBotInfoRequest(
            lang_code="",
            bot=target,
            name=copied_name,
            about=copied_about,
            description=copied_description,
        )
    )
    print("  copied name, about text, and description")

    with tempfile.TemporaryDirectory(prefix="telegram-bot-transfer-") as temp_dir:
        photo_path = await client.download_profile_photo(
            source_entity,
            file=str(Path(temp_dir) / "source-photo"),
            download_big=True,
        )
        if photo_path:
            uploaded = await client.upload_file(photo_path)
            await client(
                functions.photos.UploadProfilePhotoRequest(bot=target, file=uploaded)
            )
            print("  copied profile picture")
        else:
            print("  source has no profile picture; target picture was left unchanged")

    source_commands = await get_source_commands(
        client, source_name, api_id, api_hash
    )
    commands = [
        types.BotCommand(
            command=item.command,
            description=replace_bot_usernames(item.description, mappings),
        )
        for item in source_commands
    ]
    await copy_commands(client, target_name, commands)
    print(f"  copied {len(commands)} default command(s)")
    await regenerate_token(client, target_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy public BotFather profile settings between bots you own."
    )
    parser.add_argument(
        "items",
        nargs="+",
        metavar="ITEM",
        help="bot mappings followed by optional channel/group usernames",
    )
    return parser.parse_args()


def main() -> int:
    try:
        asyncio.run(transfer(parse_args()))
        return 0
    except (ValueError, RuntimeError, RPCError, OSError) as exc:
        print(f"Transfer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
