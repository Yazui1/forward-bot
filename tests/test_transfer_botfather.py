from __future__ import annotations

import asyncio
import unittest
from collections import deque
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
    bot_owned_by,
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
        self.chat_id = 1
        self.id = 1
        self.on_click = None

    async def click(
        self, row: int, column: int, *, password: str | None = None
    ) -> object:
        self.passwords.append(password)
        if self.on_click is not None:
            await self.on_click()
        return object()


class FakeConversation:
    def __init__(self, responses: list[FakeMessage], edits: list[FakeMessage]) -> None:
        self.responses = deque(responses)
        self.edits = iter(edits)
        self.sent: list[str] = []
        self.edited_messages: list[FakeMessage] = []
        self.on_send = {}

    async def __aenter__(self) -> FakeConversation:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def send_message(self, text: str) -> None:
        self.sent.append(text)
        callback = self.on_send.get(text)
        if callback is not None:
            await callback()

    async def get_response(self, _message: FakeMessage | None = None) -> FakeMessage:
        return self.responses.popleft()

    async def get_edit(self, message: FakeMessage) -> FakeMessage:
        self.edited_messages.append(message)
        return next(self.edits)


class FakeClient:
    def __init__(self, conversation: FakeConversation) -> None:
        self._conversation = conversation
        self.handlers = []

    def conversation(self, *_args: object, **_kwargs: object) -> FakeConversation:
        return self._conversation

    def add_event_handler(self, handler: object, _event: object) -> None:
        self.handlers.append(handler)

    def remove_event_handler(self, handler: object) -> None:
        self.handlers = [registered for registered in self.handlers if registered is not handler]

    async def emit(self, message: FakeMessage, *, new_message: bool) -> None:
        if new_message:
            self._conversation.responses.append(message)
        event = SimpleNamespace(message=message, chat_id=message.chat_id)
        for handler in tuple(self.handlers):
            await handler(event)


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

    async def test_transfer_handles_edits_and_new_messages_at_each_step(self) -> None:
        for new_messages in (False, True):
            with self.subTest(new_messages=new_messages):
                listing = FakeMessage("Choose a bot", ("Example @samplebot", False))
                listing.id = 10
                menu = FakeMessage("What do you want to do?", ("Transfer Ownership", False))
                recipient_choice = FakeMessage(
                    "Transfer bot ownership", ("Choose recipient", False))
                recipient_prompt = FakeMessage("Please share the new owner's username.")
                confirmation = FakeMessage(
                    "Transfer ownership to @owner?",
                    ("Yes, I am sure, proceed.", False),
                )
                final = FakeMessage("It worked! The bot will enjoy its new home.")
                conversation = FakeConversation([FakeMessage("cancelled"), listing], [])
                client = FakeClient(conversation)

                def transition(source: FakeMessage, response: FakeMessage) -> None:
                    async def emit_response() -> None:
                        response.id = source.id + int(new_messages)
                        await client.emit(response, new_message=new_messages)

                    source.on_click = emit_response

                transition(listing, menu)
                transition(menu, recipient_choice)

                async def choose_recipient() -> None:
                    recipient_prompt.id = recipient_choice.id + 1
                    await client.emit(recipient_prompt, new_message=True)

                recipient_choice.on_click = choose_recipient
                transition(confirmation, final)

                async def send_owner() -> None:
                    confirmation.id = recipient_prompt.id + int(new_messages)
                    await client.emit(confirmation, new_message=new_messages)

                conversation.on_send["@owner"] = send_owner

                await transfer_ownership(
                    client, "@samplebot", "@owner", password="123456"
                )

                self.assertEqual(conversation.sent, ["/cancel", "/mybots", "@owner"])
                self.assertEqual(recipient_choice.passwords, [None])
                self.assertEqual(confirmation.passwords, ["123456"])

    async def test_transfer_fails_clearly_when_2fa_password_is_missing(self) -> None:
        confirmation = FakeMessage("Confirm", ("Yes, I am sure", True))

        with self.assertRaisesRegex(RecoveryError, "creator.password"):
            await click_button(confirmation, "yes, i am sure")

    async def test_owned_check_requires_exact_botfather_list_entry(self) -> None:
        exact = FakeConversation(
            [FakeMessage("cancelled"), FakeMessage("bots", ("@samplebot", False))], []
        )
        prefix_only = FakeConversation(
            [FakeMessage("cancelled"), FakeMessage("bots", ("@samplebotextra", False))], []
        )

        self.assertTrue(await bot_owned_by(FakeClient(exact), "@samplebot"))
        self.assertFalse(await bot_owned_by(FakeClient(prefix_only), "@samplebot"))

    async def test_saved_backup_is_requeued_for_transfer_on_restart(self) -> None:
        mapping = BotMapping(
            "Example", "Example bot", Path("bot.yml"), "telegram.token", ("restart",)
        )
        service = Service.__new__(Service)
        service.config = SimpleNamespace(
            automatic_recovery=True, prepare_bots=True, bots=[mapping]
        )
        service.lock = asyncio.Lock()
        service.backups = {"example": ["@samplebot"]}
        service.pending_creations = {}
        service._save_state = Mock()
        service.log = Mock()
        service._create_backup = AsyncMock(side_effect=RecoveryError("retry later"))

        await service._reconcile_prepared_bots()

        self.assertEqual(service.backups, {})
        self.assertEqual(service.pending_creations, {"example": "@samplebot"})
        service._create_backup.assert_awaited_once_with(mapping)
        service._save_state.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
