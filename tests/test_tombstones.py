from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram.error import TelegramError

from forward_bot.features.queue_system import DeliveryQueue
from forward_bot.features.tombstones import _tombstone_delivery, remove_message


def delivery(
    delivery_id: int = 1,
    recipient_id: int = 10,
    telegram_message_id: int = 20,
):
    return SimpleNamespace(
        id=delivery_id,
        recipient_id=recipient_id,
        telegram_message_id=telegram_message_id,
        deleted=False,
        tombstone_message_id=None,
        tombstone_kind=None,
    )


def store_for(*deliveries):
    store = SimpleNamespace(delivery_queue=None)

    def mark_deleted(delivery_id, *, tombstone_message_id=None, kind="deleted"):
        target = next(item for item in deliveries if item.id == delivery_id)
        target.deleted = True
        target.tombstone_message_id = tombstone_message_id
        target.tombstone_kind = kind

    store.mark_delivery_deleted = Mock(side_effect=mark_deleted)
    return store


class TombstoneTests(unittest.IsolatedAsyncioTestCase):
    def test_delivery_queue_defaults_to_concurrent_workers(self) -> None:
        config = SimpleNamespace(get=Mock(side_effect=lambda _key, default: default))

        queue = DeliveryQueue(config, None, SimpleNamespace(), None)

        self.assertEqual(queue.worker_count, 32)
        self.assertEqual(queue.rate_per_second, 25.0)

    async def test_sticker_skips_edit_and_is_deleted_once(self) -> None:
        item = delivery()
        store = store_for(item)
        rate_wait = AsyncMock()
        store.delivery_queue = SimpleNamespace(wait_for_global_rate=rate_wait)
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(),
            delete_message=AsyncMock(),
        )

        removed = await _tombstone_delivery(
            bot,
            store,
            item,
            "removed",
            content_type="sticker",
        )

        self.assertTrue(removed)
        bot.edit_message_text.assert_not_awaited()
        bot.edit_message_media.assert_not_awaited()
        bot.delete_message.assert_awaited_once_with(chat_id=10, message_id=20)
        rate_wait.assert_awaited_once()
        self.assertEqual(item.tombstone_kind, "deleted")

    async def test_supported_content_attempts_one_edit_then_deletes(self) -> None:
        item = delivery()
        store = store_for(item)
        rate_wait = AsyncMock()
        store.delivery_queue = SimpleNamespace(wait_for_global_rate=rate_wait)
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(side_effect=TelegramError("edit failed")),
            delete_message=AsyncMock(),
        )

        removed = await _tombstone_delivery(
            bot,
            store,
            item,
            "removed",
            content_type="photo",
        )

        self.assertTrue(removed)
        bot.edit_message_media.assert_awaited_once()
        bot.delete_message.assert_awaited_once_with(chat_id=10, message_id=20)
        self.assertEqual(rate_wait.await_count, 2)

    async def test_remove_message_deduplicates_identical_delivery_targets(self) -> None:
        first = delivery(1)
        duplicate = delivery(2)
        store = store_for(first, duplicate)
        message = SimpleNamespace(
            deleted=False,
            removed_for_mods=False,
            deletion_reason=None,
            sender_id=None,
            content_type="sticker",
        )
        store.get_message = Mock(return_value=message)
        store.deliveries_for_message = Mock(return_value=[first, duplicate])
        repo = SimpleNamespace(
            get_user=Mock(return_value=SimpleNamespace(is_mod_or_admin=False))
        )
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(),
            delete_message=AsyncMock(),
        )

        count = await remove_message(
            bot,
            repo,
            store,
            None,
            30,
            reason="deleted by moderator",
            notify_sender=False,
            send_note=False,
        )

        self.assertEqual(count, 1)
        bot.delete_message.assert_awaited_once_with(chat_id=10, message_id=20)
        self.assertTrue(first.deleted)
        self.assertTrue(duplicate.deleted)

    async def test_remove_message_processes_unique_targets_concurrently(self) -> None:
        first = delivery(1, 10, 20)
        second = delivery(2, 11, 21)
        store = store_for(first, second)
        store.get_message = Mock(return_value=SimpleNamespace(
            deleted=False,
            removed_for_mods=False,
            deletion_reason=None,
            sender_id=None,
            content_type="sticker",
        ))
        store.deliveries_for_message = Mock(return_value=[first, second])
        repo = SimpleNamespace(
            get_user=Mock(return_value=SimpleNamespace(is_mod_or_admin=False))
        )
        both_started = asyncio.Event()
        started: set[tuple[int, int]] = set()

        async def delete_message(*, chat_id: int, message_id: int) -> None:
            started.add((chat_id, message_id))
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)

        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(),
            delete_message=AsyncMock(side_effect=delete_message),
        )

        count = await remove_message(
            bot,
            repo,
            store,
            None,
            30,
            reason="deleted by moderator",
            notify_sender=False,
            send_note=False,
        )

        self.assertEqual(count, 2)
        self.assertEqual(started, {(10, 20), (11, 21)})

    async def test_queued_sticker_skips_edit_and_is_deleted_once(self) -> None:
        item = delivery()
        store = store_for(item)
        queue = object.__new__(DeliveryQueue)
        queue.store = store
        queue._aggregate_logger = None
        queue.wait_for_global_rate = AsyncMock()
        queue._bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(),
            delete_message=AsyncMock(),
        )

        await queue._tombstone_existing_delivery(item, None, "sticker")

        queue._bot.edit_message_text.assert_not_awaited()
        queue._bot.edit_message_media.assert_not_awaited()
        queue._bot.delete_message.assert_awaited_once_with(10, 20)
        queue.wait_for_global_rate.assert_awaited_once()
        self.assertEqual(item.tombstone_kind, "deleted")


if __name__ == "__main__":
    unittest.main()
