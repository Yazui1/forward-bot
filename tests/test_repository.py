from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from forward_bot.db.repository import Repository
from forward_bot.features.credits import inflation_rate
from forward_bot.utils import iso, now_utc, today_key


class RepositoryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "bot.db"
        self.repo = Repository(self.db_path)

    def add_user(self, user_id: int, credits: float = 20.0):
        user, created = self.repo.ensure_user(user_id, f"user{user_id}", credits)
        self.assertTrue(created)
        return user

    def scalar(self, sql: str, parameters: tuple = ()):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(sql, parameters).fetchone()
        return row[0] if row else None

    def test_ensure_user_uses_cache_when_identity_is_unchanged(self) -> None:
        expected = self.add_user(1)

        with patch.object(
            self.repo,
            "connect",
            side_effect=AssertionError("unchanged user should not access SQLite"),
        ):
            actual, created = self.repo.ensure_user(1, "user1", 20.0)

        self.assertIs(actual, expected)
        self.assertFalse(created)

    def test_activity_is_staged_and_flushed_in_one_batch(self) -> None:
        first = self.add_user(1)
        second = self.add_user(2)

        self.repo.touch_activity(first.telegram_id)
        self.repo.touch_activity(second.telegram_id)
        self.assertIsNone(
            self.scalar("SELECT last_activity FROM users WHERE telegram_id=1")
        )

        self.assertEqual(self.repo.flush_activity(), 2)
        self.assertEqual(self.repo.flush_activity(), 0)
        self.assertEqual(
            self.scalar("SELECT last_activity FROM users WHERE telegram_id=1"),
            first.last_activity,
        )

    def test_credit_change_persists_staged_activity_in_same_transaction(self) -> None:
        user = self.add_user(1)
        self.repo.touch_activity(user.telegram_id)

        applied, updated = self.repo.apply_credit_change(
            user.telegram_id,
            2.5,
            "test_reward",
            record_activity=True,
        )

        self.assertEqual(applied, 2.5)
        self.assertEqual(updated.credits, 22.5)
        self.assertEqual(self.repo.flush_activity(), 0)
        with sqlite3.connect(self.db_path) as conn:
            credits, activity = conn.execute(
                "SELECT credits, last_activity FROM users WHERE telegram_id=1"
            ).fetchone()
            dead_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credit_daily_net'"
            ).fetchone()
        self.assertEqual(credits, 22.5)
        self.assertEqual(activity, updated.last_activity)
        self.assertIsNone(dead_table)

    def test_daily_tax_updates_all_users_with_one_connection(self) -> None:
        first = self.add_user(1, 100.0)
        second = self.add_user(2, 50.0)
        zero = self.add_user(3, 0.0)
        original_connect = self.repo.connect
        connection_count = 0

        @contextmanager
        def counted_connect():
            nonlocal connection_count
            connection_count += 1
            with original_connect() as conn:
                yield conn

        with patch.object(self.repo, "connect", counted_connect):
            applied = self.repo.apply_daily_taxes(
                [(first, 0.10), (second, 0.20), (zero, 0.10)]
            )
            repeated = self.repo.apply_daily_taxes(
                [(first, 0.10), (second, 0.20), (zero, 0.10)]
            )

        self.assertEqual(connection_count, 1)
        self.assertEqual(len(applied), 3)
        self.assertEqual(repeated, [])
        self.assertEqual(first.credits, 90.0)
        self.assertEqual(second.credits, 40.0)
        self.assertEqual(zero.last_daily_tax_date, today_key())
        self.assertEqual(
            self.scalar(
                "SELECT net_amount FROM credit_global_daily WHERE day=?",
                (today_key(),),
            ),
            130.0,
        )

    def test_inflation_uses_opening_supply_and_rejects_invalid_denominators(self) -> None:
        self.add_user(1, 100.0)
        self.add_user(2, 50.0)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET is_banned=1 WHERE telegram_id=2")
            conn.commit()

        self.assertEqual(self.repo.current_credit_supply(), 150.0)
        self.assertEqual(self.repo.net_issuance_since_days(1), 150.0)
        self.assertAlmostEqual(inflation_rate(150.0, 50.0), 0.5)
        self.assertAlmostEqual(inflation_rate(80.0, -20.0), -0.2)
        self.assertIsNone(inflation_rate(10.0, 10.0))
        self.assertIsNone(inflation_rate(5.0, 10.0))
        self.assertIsNone(inflation_rate(float("nan"), 1.0))

    def test_duplicate_hash_does_not_write_until_window_expires(self) -> None:
        with sqlite3.connect(self.db_path) as observer:
            first, duplicate = self.repo.claim_media_hash("digest", 3)
            version_after_insert = observer.execute("PRAGMA data_version").fetchone()[0]
            repeated, duplicate_again = self.repo.claim_media_hash("digest", 3)
            version_after_repeat = observer.execute("PRAGMA data_version").fetchone()[0]

            self.assertFalse(duplicate)
            self.assertTrue(duplicate_again)
            self.assertEqual(first["first_seen_at"], repeated["first_seen_at"])
            self.assertEqual(version_after_insert, version_after_repeat)

            expired = iso(now_utc() - timedelta(days=4))
            observer.execute(
                "UPDATE media_hashes SET first_seen_at=? WHERE hash=?",
                (expired, "digest"),
            )
            observer.commit()

        renewed, duplicate_after_expiry = self.repo.claim_media_hash("digest", 3)
        _, duplicate_after_renewal = self.repo.claim_media_hash("digest", 3)
        self.assertFalse(duplicate_after_expiry)
        self.assertTrue(duplicate_after_renewal)
        self.assertNotEqual(renewed["first_seen_at"], expired)


class SchemaMigrationTests(unittest.TestCase):
    def test_credit_tables_are_compact_and_old_rows_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            old_day = (now_utc().date() - timedelta(days=30)).isoformat()
            today = today_key()
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE credit_daily_earnings (
                        user_id INTEGER NOT NULL,
                        day TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        positive_amount REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, day, reason)
                    );
                    CREATE TABLE credit_daily_net (
                        user_id INTEGER NOT NULL,
                        day TEXT NOT NULL,
                        net_amount REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, day)
                    );
                    CREATE TABLE credit_global_daily (
                        day TEXT PRIMARY KEY,
                        net_amount REAL NOT NULL DEFAULT 0
                    );
                    CREATE TABLE media_hashes (
                        hash TEXT PRIMARY KEY,
                        first_seen_at TEXT NOT NULL,
                        latest_seen_at TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO credit_daily_earnings VALUES (?, ?, ?, ?)",
                    (1, old_day, "old", 1.0),
                )
                conn.execute(
                    "INSERT INTO credit_daily_earnings VALUES (?, ?, ?, ?)",
                    (1, today, "current", 2.0),
                )
                conn.execute(
                    "INSERT INTO credit_daily_net VALUES (?, ?, ?)",
                    (1, today, 2.0),
                )
                conn.execute(
                    "INSERT INTO credit_global_daily VALUES (?, ?)",
                    (old_day, 1.0),
                )
                conn.execute(
                    "INSERT INTO credit_global_daily VALUES (?, ?)",
                    (today, 2.0),
                )
                conn.execute(
                    "INSERT INTO media_hashes VALUES (?, ?, ?)",
                    ("digest", old_day, today),
                )

            Repository(db_path)

            with sqlite3.connect(db_path) as conn:
                schemas = {
                    name: sql.upper()
                    for name, sql in conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='table'"
                    )
                }
                earnings = conn.execute(
                    "SELECT day, user_id, reason, positive_amount FROM credit_daily_earnings"
                ).fetchall()
                global_rows = conn.execute(
                    "SELECT day, net_amount FROM credit_global_daily"
                ).fetchall()

            self.assertNotIn("credit_daily_net", schemas)
            self.assertIn("WITHOUT ROWID", schemas["credit_daily_earnings"])
            self.assertIn(
                "PRIMARY KEY (DAY, USER_ID, REASON)",
                schemas["credit_daily_earnings"],
            )
            self.assertIn("WITHOUT ROWID", schemas["credit_global_daily"])
            self.assertIn("WITHOUT ROWID", schemas["media_hashes"])
            self.assertNotIn("LATEST_SEEN_AT", schemas["media_hashes"])
            self.assertEqual(earnings, [(today, 1, "current", 2.0)])
            self.assertEqual(global_rows, [(today, 2.0)])


if __name__ == "__main__":
    unittest.main()
