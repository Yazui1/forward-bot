from __future__ import annotations

import sqlite3
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
    first_seen_at TEXT NOT NULL,
    latest_seen_at TEXT NOT NULL
);

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
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    reason TEXT NOT NULL,
    positive_amount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, reason)
);

CREATE TABLE IF NOT EXISTS credit_daily_net (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    net_amount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS credit_global_daily (
    day TEXT PRIMARY KEY,
    net_amount REAL NOT NULL DEFAULT 0
);
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
)


def init_schema(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for table in DISALLOWED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
