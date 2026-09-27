from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from transfer.transfer import (
    AutomaticWaitExceeded,
    BotMapping,
    Config,
    Recovery,
    RecoveryError,
    Service,
    click_button,
    create_bot,
    transfer_ownership,
)


class FakeMessage:
    def __init__(self, text: str, *buttons: tuple[str, bool]) -> None:
        self.raw_text = text
        self.buttons = [[
            SimpleNamespace(
                text=label,
                button=SimpleNamespace(requires_password=requires_password),
            )
            for label, requires_password in buttons
        ]] if buttons else []
        self.passwords: list[str | None] = []

    async def click(
        self, row: int, column: int, *, password: str | None = None
    ) -> None:
        self.passwords.append(password)


class FakeConversation:
    def __init__(self, responses: list[FakeMessage], edits: list[FakeMessage]) -> None:
        self.responses = iter(responses)
        self.edits = iter(edits)
        self.sent: list[str] = []
        self.edited_messages: list[FakeMessage] = []

    async def __aenter__(self) -> FakeConversation:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def get_response(self) -> FakeMessage:
        return next(self.responses)

    async def get_edit(self, message: FakeMessage) -> FakeMessage:
        self.edited_messages.append(message)
        return next(self.edits)


class FakeClient:
    def __init__(self, conversation: FakeConversation) -> None:
        self._conversation = conversation

    def conversation(self, *_args: object, **_kwargs: object) -> FakeConversation:
        return self._conversation


class BotFatherTests(unittest.IsolatedAsyncioTestCase):
    def test_creator_password_is_read_as_a_string(self) -> None:
        value = {
            "telegram": {
                "api_id": "1",
                "api_hash": "hash",
                "main": {"username": "@main", "session": "main.session"},
            },
            "announcement": {
                "channel": "@channel",
                "group": "@group",
                "recovery_template": "{friendly_name}",
                "restored_template": "{new_handle}",
            },
            "cloudflare": {},
            "automatic_recovery": {
                "enabled": True,
                "prepare_bots": True,
                "creator": {
                    "username": "@creator",
                    "session": "creator.session",
                    "api_id": "1",
                    "api_hash": "hash",
                    "password": "123456",
                },
            },
            "bots": [{
                "handle_prefix": "Example",
                "friendly_name": "Example bot",
                "config_path": "bot.yml",
                "token_path": "telegram.token",
                "restart_command": ["restart"],
            }],
        }

        config = Config(value, Path("config.yml"))

        self.assertEqual(config.creator_password, "123456")

    async def test_long_automatic_wait_switches_recovery_to_manual(self) -> None:
        service = Service.__new__(Service)
        service.lock = asyncio.Lock()
        service.recoveries = {}
        service._save_state = Mock()
        service.log = Mock()
        service._run_automatic_recovery = AsyncMock(
            side_effect=AutomaticWaitExceeded("creator wait exceeds limit"))
        service._prepare_recovery = AsyncMock()
        recovery = Recovery(
            key="example",
            old_handle="@oldbot",
            receiver_username="@main",
            anonymous_link="",
            stage="automatic_preparing",
        )
        mapping = BotMapping(
            "Example", "Example bot", Path("bot.yml"), "telegram.token", ("restart",)
        )
        service.recoveries[recovery.key] = recovery

        await service._run_automatic_or_fallback(recovery, mapping)

        self.assertEqual(recovery.stage, "starting")
        service._prepare_recovery.assert_awaited_once_with(recovery, mapping)

    async def test_create_bot_sends_configured_name_before_username(self) -> None:
        conversation = FakeConversation(
            [
                FakeMessage("cancelled"),
                FakeMessage("new bot name"),
                FakeMessage("choose username"),
                FakeMessage("Use this token: 12345:abcdefghijklmnopqrstuvwxyz"),
            ],
            [],
        )

        handle = await create_bot(FakeClient(conversation), "ExampleRelay", "Example relay")

        self.assertRegex(handle, r"^@ExampleRelay\d{2}bot$")
        self.assertEqual(conversation.sent[:3], ["/cancel", "/newbot", "Example relay"])
        self.assertEqual(conversation.sent[-1], handle[1:])

    async def test_transfer_follows_botfather_edits_and_confirms_with_2fa(self) -> None:
        listing = FakeMessage("Choose a bot", ("Example @samplebot", False))
        menu = FakeMessage("What do you want to do?", ("Transfer Ownership", False))
        transfer_prompt = FakeMessage(
            "Transfer bot ownership", ("Choose recipient", False))
        confirmation = FakeMessage(
            "Transfer ownership to @owner?",
            ("Yes, I am sure, proceed.", True),
        )
        final = FakeMessage("It worked! The bot will enjoy its new home.")
        conversation = FakeConversation(
            [
                FakeMessage("cancelled"),
                listing,
                transfer_prompt,
                FakeMessage("Please share the new owner's username."),
                confirmation,
            ],
            [menu, final],
        )

        await transfer_ownership(
            FakeClient(conversation), "@samplebot", "@owner", password="123456"
        )

        self.assertEqual(conversation.sent, ["/cancel", "/mybots", "@owner"])
        self.assertEqual(conversation.edited_messages, [listing, confirmation])
        self.assertEqual(confirmation.passwords, ["123456"])

    async def test_transfer_fails_clearly_when_2fa_password_is_missing(self) -> None:
        confirmation = FakeMessage("Confirm", ("Yes, I am sure", True))

        with self.assertRaisesRegex(RecoveryError, "creator.password"):
            await click_button(confirmation, "yes, i am sure")


if __name__ == "__main__":
    unittest.main()
