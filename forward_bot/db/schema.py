from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    has_started INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_activity TEXT,
    is_banned INTEGER NOT NULL DEFAULT 0,
    is_moderator INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    confirmation_enabled INTEGER NOT NULL DEFAULT 1,
    votes_enabled INTEGER NOT NULL DEFAULT 1,
    vote_buttons_enabled INTEGER NOT NULL DEFAULT 1,
    hide_potentially_unwanted INTEGER NOT NULL DEFAULT 0,
    filter_duplicates INTEGER NOT NULL DEFAULT 1,
    preserve_forwards INTEGER NOT NULL DEFAULT 0,
    fights_enabled INTEGER NOT NULL DEFAULT 1,
    sign_enabled INTEGER NOT NULL DEFAULT 0,
    tripcode_enabled INTEGER NOT NULL DEFAULT 0,
    tripcode_name TEXT,
    tripcode_hash TEXT,
    warning_count INTEGER NOT NULL DEFAULT 0,
    upvotes_received INTEGER NOT NULL DEFAULT 0,
    downvotes_received INTEGER NOT NULL DEFAULT 0,
    credits REAL NOT NULL DEFAULT 0,
    about_seen INTEGER NOT NULL DEFAULT 0,
    onboarding_acknowledged INTEGER NOT NULL DEFAULT 0,
    onboarding_question_index INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    cooldown_reason TEXT,
    cooldown_applied_by INTEGER,
    downvote_streak INTEGER NOT NULL DEFAULT 0,
    last_downvote_at TEXT,
    last_daily_tax_date TEXT
);

CREATE TABLE IF NOT EXISTS bot_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_started ON users(has_started, is_banned);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS media_hashes (
    hash TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS user_blocks (
    blocker_id INTEGER NOT NULL,
    blocked_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (blocker_id, blocked_id),
    FOREIGN KEY (blocker_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
    FOREIGN KEY (blocked_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS blocked_sticker_sets (
    set_name TEXT PRIMARY KEY,
    blocked_by INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    invite_code TEXT PRIMARY KEY,
    inviter_id INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (inviter_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invite_redemptions (
    invite_code TEXT NOT NULL,
    invitee_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (invite_code, invitee_id),
    FOREIGN KEY (invite_code) REFERENCES invites(invite_code) ON DELETE CASCADE,
    FOREIGN KEY (invitee_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS credit_daily_earnings (
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    positive_amount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, reason)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS credit_global_daily (
    day TEXT PRIMARY KEY,
    net_amount REAL NOT NULL DEFAULT 0
) WITHOUT ROWID;
"""


DISALLOWED_TABLES = (
    "credit_transactions",
    "credit_tax_runs",
    "user_downvote_state",
    "cooldowns",
    "cooldown_history",
    "about_state",
    "messages",
    "deliveries",
    "votes",
    "whispers",
    "fights",
    "sauce_cache",
    "blocks",
    "schema_migrations",
    "credit_daily_net",
)


def init_schema(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_SQL)
        for table in DISALLOWED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        _ensure_user_columns(conn)
        _prune_credit_aggregates(conn)
        _migrate_media_hashes(conn)
        _migrate_credit_tables(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_user_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "onboarding_acknowledged" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN onboarding_acknowledged INTEGER NOT NULL DEFAULT 1"
        )
    if "onboarding_question_index" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN onboarding_question_index INTEGER NOT NULL DEFAULT 0"
        )
    if "preserve_forwards" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN preserve_forwards INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_credit_tables(conn: sqlite3.Connection) -> None:
    earnings_sql = _table_sql(conn, "credit_daily_earnings")
    compact_earnings = "WITHOUT ROWID" in earnings_sql.upper()
    day_first = "PRIMARY KEY (DAY, USER_ID, REASON)" in earnings_sql.upper()
    if not compact_earnings or not day_first:
        conn.execute("DROP TABLE IF EXISTS credit_daily_earnings_new")
        conn.execute(
            """
            CREATE TABLE credit_daily_earnings_new (
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                positive_amount REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (day, user_id, reason)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            INSERT INTO credit_daily_earnings_new (day, user_id, reason, positive_amount)
            SELECT day, user_id, reason, positive_amount FROM credit_daily_earnings
            """
        )
        conn.execute("DROP TABLE credit_daily_earnings")
        conn.execute(
            "ALTER TABLE credit_daily_earnings_new RENAME TO credit_daily_earnings"
        )

    global_sql = _table_sql(conn, "credit_global_daily")
    if "WITHOUT ROWID" not in global_sql.upper():
        conn.execute("DROP TABLE IF EXISTS credit_global_daily_new")
        conn.execute(
            """
            CREATE TABLE credit_global_daily_new (
                day TEXT PRIMARY KEY,
                net_amount REAL NOT NULL DEFAULT 0
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            INSERT INTO credit_global_daily_new (day, net_amount)
            SELECT day, net_amount FROM credit_global_daily
            """
        )
        conn.execute("DROP TABLE credit_global_daily")
        conn.execute("ALTER TABLE credit_global_daily_new RENAME TO credit_global_daily")


def _migrate_media_hashes(conn: sqlite3.Connection) -> None:
    media_sql = _table_sql(conn, "media_hashes")
    if "WITHOUT ROWID" in media_sql.upper() and "LATEST_SEEN_AT" not in media_sql.upper():
        return
    conn.execute("DROP TABLE IF EXISTS media_hashes_new")
    conn.execute(
        """
        CREATE TABLE media_hashes_new (
            hash TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        INSERT INTO media_hashes_new (hash, first_seen_at)
        SELECT hash, first_seen_at FROM media_hashes
        """
    )
    conn.execute("DROP TABLE media_hashes")
    conn.execute("ALTER TABLE media_hashes_new RENAME TO media_hashes")


def _prune_credit_aggregates(conn: sqlite3.Connection) -> None:
    today = datetime.now(UTC).date()
    conn.execute(
        "DELETE FROM credit_daily_earnings WHERE day < ?", (today.isoformat(),)
    )
    conn.execute(
        "DELETE FROM credit_global_daily WHERE day < ?",
        ((today - timedelta(days=6)).isoformat(),),
    )


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return str(row[0] or "") if row else ""
