from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

from transfer.transfer import (
    BotFatherMiniApp,
    create_bot,
    load_config,
    make_client,
    normalize_username,
)


@unittest.skipUnless(
    os.environ.get("RUN_BOTFATHER_MINIAPP_CREATE_TEST") == "1",
    "live Telegram bot creation test is opt-in",
)
class BotFatherMiniAppLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_cookie_is_reused_to_create_bot(self) -> None:
        config = load_config(Path(__file__).resolve().parents[1]
                             / "transfer" / "config.yml")
        if config.creator is None:
            self.fail("automatic_recovery.creator is not configured")

        client = make_client(
            config.creator,
            config,
            config.creator_api_id,
            config.creator_api_hash,
            flood_sleep_threshold=0,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.fail("creator session is not authorized")
            me = await client.get_me()
            username = getattr(me, "username", None)
            actual = normalize_username(username) if username else ""
            if actual.casefold() != config.creator.username.casefold():
                self.fail("creator session does not match the configured account")

            app = BotFatherMiniApp(client)
            await app._refresh_auth()
            self.assertTrue(app._has_auth_cookie(),
                            "BotFather auth did not set its session cookie")

            # Verify that creation can use the persisted hash and cookie from a fresh instance.
            cached_app = BotFatherMiniApp(client)
            self.assertTrue(cached_app.api_hash)
            self.assertTrue(cached_app._has_auth_cookie())

            prefix = f"fwdmini{secrets.randbelow(100_000_000):08d}"
            handle = await create_bot(
                client, prefix, "ForwardBotMiniAppTest")
            self.assertTrue(handle.lstrip("@").casefold().startswith(prefix))
            print(f"Created Mini App test bot: {handle}", flush=True)
        finally:
            await client.disconnect()


if __name__ == "__main__":
    unittest.main()
