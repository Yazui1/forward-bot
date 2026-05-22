from __future__ import annotations

import json

import aiosqlite

from forward_bot.crypto.tripcode import hash_tripcode


async def migrate_if_needed(db: aiosqlite.Connection, schema_sql: str, global_salt: str = "") -> None:
    columns = await _table_columns(db, "users")
    if not columns or "telegram_id" in columns or "id" not in columns:
        await migrate_tripcodes_if_needed(db, global_salt)
        return

    await db.execute("ALTER TABLE users RENAME TO legacy_secretlounge_users")
    await db.executescript(schema_sql)

    await db.execute(
        """
        INSERT OR IGNORE INTO users (
            telegram_id,
            username,
            has_started,
            is_banned,
            is_moderator,
            is_admin,
            created_at,
            last_activity,
            confirmation_enabled,
            votes_enabled,
            vote_buttons_enabled,
            hide_potentially_unwanted,
            fights_enabled,
            sign_enabled,
            tripcode_enabled,
            tripcode_name,
            tripcode_hash,
            warning_count,
            upvotes_received,
            downvotes_received,
            credits
        )
        SELECT
            id,
            username,
            CASE WHEN "left" IS NULL THEN 1 ELSE 0 END,
            CASE WHEN blacklistReason IS NOT NULL THEN 1 ELSE 0 END,
            CASE WHEN rank > 0 THEN 1 ELSE 0 END,
            CASE WHEN rank >= 100 THEN 1 ELSE 0 END,
            COALESCE(joined, CURRENT_TIMESTAMP),
            lastActive,
            sendconfirm,
            CASE WHEN hideVoting = 1 THEN 0 ELSE 1 END,
            votebutton,
            CASE WHEN showPotentiallyUnwanted = 1 THEN 0 ELSE 1 END,
            1,
            signenabled,
            tsignenabled,
            CASE
                WHEN tripcode IS NOT NULL AND instr(tripcode, '#') > 0
                THEN substr(tripcode, 1, instr(tripcode, '#') - 1)
                ELSE NULL
            END,
            NULL,
            warnings,
            creditsUpvoteCount,
            creditsDownvoteCount,
            ROUND(credits, 2)
        FROM legacy_secretlounge_users
        """
    )
    await db.execute(
        """
        INSERT OR REPLACE INTO cooldowns (user_id, until_at, reason, applied_by, created_at)
        SELECT id, cooldownUntil, 'legacy cooldown', NULL, CURRENT_TIMESTAMP
        FROM legacy_secretlounge_users
        WHERE cooldownUntil IS NOT NULL AND datetime(cooldownUntil) > datetime('now')
        """
    )
    await db.execute(
        """
        INSERT INTO cooldown_history (user_id, until_at, reason, applied_by, created_at)
        SELECT id, cooldownUntil, 'legacy cooldown', NULL, CURRENT_TIMESTAMP
        FROM legacy_secretlounge_users
        WHERE cooldownUntil IS NOT NULL
        """
    )
    await db.execute(
        """
        INSERT OR IGNORE INTO invites (invite_code, inviter_id, uses, created_at)
        SELECT u.referralCode, u.id, (
            SELECT COUNT(*) FROM legacy_secretlounge_users r WHERE r.referredBy = u.id
        ), COALESCE(u.joined, CURRENT_TIMESTAMP)
        FROM legacy_secretlounge_users u
        WHERE u.referralCode IS NOT NULL AND u.referralCode != ''
        """
    )
    await db.execute(
        """
        INSERT OR IGNORE INTO invite_redemptions (invite_code, invitee_id, created_at)
        SELECT inviter.referralCode, invitee.id, COALESCE(invitee.joined, CURRENT_TIMESTAMP)
        FROM legacy_secretlounge_users invitee
        JOIN legacy_secretlounge_users inviter ON inviter.id = invitee.referredBy
        WHERE inviter.referralCode IS NOT NULL AND inviter.referralCode != ''
        """
    )
    await _migrate_blocks(db)
    await _migrate_tripcodes_from_legacy(db, global_salt)
    await _migrate_media_hashes(db)
    await _migrate_about(db)
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (name) VALUES ('legacy_secretlounge')"
    )
    await db.commit()


async def migrate_tripcodes_if_needed(db: aiosqlite.Connection, global_salt: str = "") -> None:
    if not await _table_exists(db, "legacy_secretlounge_users"):
        return
    if await _migration_applied(db, "legacy_secretlounge_tripcode_hash_fix"):
        return
    await _migrate_tripcodes_from_legacy(db, global_salt)
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (name) VALUES ('legacy_secretlounge_tripcode_hash_fix')"
    )
    await db.commit()


async def _migrate_tripcodes_from_legacy(db: aiosqlite.Connection, global_salt: str) -> None:
    if not await _table_exists(db, "legacy_secretlounge_users"):
        return
    rows = await (await db.execute(
        """
        SELECT id, tripcode
        FROM legacy_secretlounge_users
        WHERE tripcode IS NOT NULL AND instr(tripcode, '#') > 0
        """
    )).fetchall()
    for row in rows:
        raw = str(row["tripcode"])
        name, secret = raw.split("#", 1)
        name = name.strip()
        secret = secret.strip()
        if not name or not secret:
            continue
        await db.execute(
            """
            UPDATE users
            SET tripcode_name = ?, tripcode_hash = ?
            WHERE telegram_id = ?
            """,
            (name, hash_tripcode(secret, global_salt), int(row["id"])),
        )


async def _migrate_blocks(db: aiosqlite.Connection) -> None:
    rows = await (await db.execute(
        "SELECT id, blockedUserIds FROM legacy_secretlounge_users WHERE blockedUserIds IS NOT NULL AND blockedUserIds != '[]'"
    )).fetchall()
    for row in rows:
        for blocked_id in _parse_json_ints(row["blockedUserIds"]):
            await db.execute(
                "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)",
                (int(row["id"]), blocked_id),
            )


async def _migrate_media_hashes(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, "image_hashes"):
        return
    await db.execute(
        """
        INSERT INTO media_hashes (hash, created_at)
        SELECT phash, COALESCE(created_at, CURRENT_TIMESTAMP)
        FROM image_hashes
        WHERE phash IS NOT NULL AND phash != ''
        """
    )


async def _migrate_about(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, "system_config"):
        return
    row = await (await db.execute(
        "SELECT value FROM system_config WHERE name = 'motd' AND value != ''"
    )).fetchone()
    if row is not None:
        await db.execute(
            "UPDATE about_state SET message = ? WHERE id = 1",
            (row["value"],),
        )


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    row = await (await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )).fetchone()
    return row is not None


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    return {str(row["name"]) for row in rows}


async def _migration_applied(db: aiosqlite.Connection, name: str) -> bool:
    if not await _table_exists(db, "schema_migrations"):
        return False
    row = await (await db.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (name,),
    )).fetchone()
    return row is not None


def _parse_json_ints(raw: str) -> list[int]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids
