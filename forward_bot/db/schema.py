from __future__ import annotations

import aiosqlite

from forward_bot.db.migrations.legacy_secretlounge import migrate_if_needed as migrate_legacy_secretlounge
from forward_bot.db.migrations.media_hashes_drop_message_id import migrate_if_needed as migrate_media_hashes_drop_message_id
from forward_bot.db.migrations.user_potentially_unwanted import migrate_if_needed as migrate_user_potentially_unwanted
from forward_bot.db.migrations.user_start_and_vote_buttons import migrate_if_needed as migrate_user_start_and_vote_buttons
from forward_bot.db.migrations.user_duplicate_filter import migrate_if_needed as migrate_user_duplicate_filter


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    has_started INTEGER NOT NULL DEFAULT 0,
    is_banned INTEGER NOT NULL DEFAULT 0,
    is_moderator INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity TEXT,
    confirmation_enabled INTEGER NOT NULL DEFAULT 1,
    votes_enabled INTEGER NOT NULL DEFAULT 1,
    vote_buttons_enabled INTEGER NOT NULL DEFAULT 1,
    hide_potentially_unwanted INTEGER NOT NULL DEFAULT 0,
    filter_duplicates INTEGER NOT NULL DEFAULT 0,
    fights_enabled INTEGER NOT NULL DEFAULT 1,
    sign_enabled INTEGER NOT NULL DEFAULT 0,
    tripcode_enabled INTEGER NOT NULL DEFAULT 0,
    tripcode_name TEXT,
    tripcode_hash TEXT,
    warning_count INTEGER NOT NULL DEFAULT 0,
    upvotes_received INTEGER NOT NULL DEFAULT 0,
    downvotes_received INTEGER NOT NULL DEFAULT 0,
    about_seen INTEGER NOT NULL DEFAULT 0,
    credits REAL NOT NULL DEFAULT 20.0
);
CREATE INDEX IF NOT EXISTS idx_users_started_banned ON users(has_started, is_banned);
CREATE INDEX IF NOT EXISTS idx_users_last_activity ON users(last_activity);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS blocks (
    blocker_id INTEGER NOT NULL,
    blocked_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (blocker_id, blocked_id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_blocker ON blocks(blocker_id);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_credit_tx_user ON credit_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_credit_tx_time ON credit_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_credit_tx_user_reason_time ON credit_transactions(user_id, reason, created_at);

CREATE TABLE IF NOT EXISTS credit_tax_runs (
    user_id INTEGER NOT NULL,
    tax_date TEXT NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, tax_date)
);

CREATE TABLE IF NOT EXISTS user_downvote_state (
    user_id INTEGER PRIMARY KEY,
    streak REAL NOT NULL DEFAULT 0.0,
    last_downvote_at TEXT
);

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id INTEGER PRIMARY KEY,
    until_at TEXT NOT NULL,
    reason TEXT,
    applied_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cooldowns_until ON cooldowns(until_at);

CREATE TABLE IF NOT EXISTS cooldown_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    until_at TEXT NOT NULL,
    reason TEXT,
    applied_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invites (
    invite_code TEXT PRIMARY KEY,
    inviter_id INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invite_redemptions (
    invite_code TEXT NOT NULL,
    invitee_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (invite_code, invitee_id)
);

CREATE TABLE IF NOT EXISTS media_hashes (
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_media_hashes_hash ON media_hashes(hash);
CREATE INDEX IF NOT EXISTS idx_media_hashes_created_at ON media_hashes(created_at);
CREATE INDEX IF NOT EXISTS idx_media_hashes_hash_created_at ON media_hashes(hash, created_at);

CREATE TABLE IF NOT EXISTS blocked_sticker_sets (
    set_name TEXT PRIMARY KEY,
    blocked_by INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS about_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    message TEXT NOT NULL
);
INSERT OR IGNORE INTO about_state (id, message) VALUES (1, 'Anonymous message relay bot.');
"""


async def init_schema(db_path: str, global_salt: str = "") -> None:
    async with aiosqlite.connect(db_path, timeout=30.0) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 30000")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await migrate_legacy_secretlounge(db, SCHEMA_SQL, global_salt)
        await db.executescript(SCHEMA_SQL)
        await migrate_media_hashes_drop_message_id(db)
        await migrate_user_potentially_unwanted(db)
        await migrate_user_start_and_vote_buttons(db)
        await migrate_user_duplicate_filter(db)
        await db.commit()
