from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forward_bot.db.schema import init_schema
from forward_bot.utils import iso, now_utc, parse_dt, round_credits


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MigrationResult:
    migrated: bool
    reason: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    about_text: str | None = None


def migrate_legacy_database(source_path: str | Path | None, target_path: str | Path) -> MigrationResult:
    if not source_path:
        return MigrationResult(False, "no source configured")
    source = Path(source_path)
    target = Path(target_path)
    if not source.exists():
        return MigrationResult(False, f"source does not exist: {source}")
    if source.resolve() == target.resolve():
        return MigrationResult(False, "source and target are the same file; use a separate new database path")

    init_schema(target)
    with closing(_connect(source)) as src, closing(_connect(target)) as dst:
        about_text = _read_legacy_about(src)
        if _table_count(dst, "users") > 0:
            return MigrationResult(False, "target already contains users", about_text=about_text)

        counts: dict[str, int] = {}
        user_ids = _migrate_users(src, dst)
        counts["users"] = len(user_ids)
        counts["media_hashes"] = _migrate_media_hashes(src, dst)
        counts["user_blocks"] = _migrate_blocks(src, dst, user_ids)
        counts["blocked_sticker_sets"] = _migrate_blocked_sticker_sets(
            src, dst)
        invite_codes = _migrate_invites(src, dst, user_ids)
        counts["invites"] = len(invite_codes)
        counts["invite_redemptions"] = _migrate_invite_redemptions(
            src, dst, user_ids, invite_codes)
        credit_counts = _migrate_credit_aggregates(src, dst, user_ids)
        counts.update(credit_counts)
        dst.commit()

    LOGGER.info("migrated legacy database from %s into %s: %s",
                source, target, counts)
    return MigrationResult(True, "migrated", counts, about_text=about_text)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _read_legacy_about(src: sqlite3.Connection) -> str | None:
    if not _table_exists(src, "system_config"):
        return None
    cols = _columns(src, "system_config")
    if not {"name", "value"}.issubset(cols):
        return None
    rows = src.execute(
        f"""
        SELECT name, value
        FROM system_config
        WHERE name = "about" 
        """,
    ).fetchall()
    for row in rows:
        if row["name"] == "about":
            return str(row["value"])

    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _row_value(row: dict[str, Any], key: str, default: Any = None) -> Any:
    value = row.get(key, default)
    return default if value is None else value


def _int_flag(value: Any, default: bool = False) -> int:
    if value is None:
        return int(default)
    return int(bool(int(value)))


def _migrate_users(src: sqlite3.Connection, dst: sqlite3.Connection) -> set[int]:
    if not _table_exists(src, "users"):
        return set()

    cooldowns = _cooldown_map(src)
    downvotes = _downvote_map(src)
    tax_dates = _tax_date_map(src)
    users: list[tuple[Any, ...]] = []
    user_ids: set[int] = set()
    for raw in src.execute("SELECT * FROM users"):
        row = dict(raw)
        user_id = int(row["telegram_id"])
        cooldown = cooldowns.get(user_id, {})
        downvote = downvotes.get(user_id, {})
        values = (
            user_id,
            row.get("username"),
            _int_flag(row.get("has_started")),
            str(_row_value(row, "created_at", iso())),
            row.get("last_activity"),
            _int_flag(row.get("is_banned")),
            _int_flag(row.get("is_moderator")),
            _int_flag(row.get("is_admin")),
            _int_flag(row.get("confirmation_enabled"), True),
            _int_flag(row.get("votes_enabled"), True),
            _int_flag(row.get("vote_buttons_enabled"), True),
            _int_flag(row.get("hide_potentially_unwanted")),
            _int_flag(row.get("filter_duplicates"), True),
            _int_flag(row.get("preserve_forwards")),
            _int_flag(row.get("fights_enabled"), True),
            _int_flag(row.get("sign_enabled")),
            _int_flag(row.get("tripcode_enabled")),
            row.get("tripcode_name"),
            row.get("tripcode_hash"),
            int(_row_value(row, "warning_count", 0)),
            int(_row_value(row, "upvotes_received", 0)),
            int(_row_value(row, "downvotes_received", 0)),
            round_credits(float(_row_value(row, "credits", 0))),
            _int_flag(row.get("about_seen")),
            0,
            0,
            cooldown.get("until_at"),
            cooldown.get("reason"),
            cooldown.get("applied_by"),
            int(float(downvote.get("streak", 0) or 0)),
            downvote.get("last_downvote_at"),
            tax_dates.get(user_id),
        )
        users.append(values)
        user_ids.add(user_id)

    dst.executemany(
        """
        INSERT OR REPLACE INTO users (
            telegram_id, username, has_started, created_at, last_activity,
            is_banned, is_moderator, is_admin,
            confirmation_enabled, votes_enabled, vote_buttons_enabled,
            hide_potentially_unwanted, filter_duplicates, preserve_forwards, fights_enabled,
            sign_enabled, tripcode_enabled, tripcode_name, tripcode_hash,
            warning_count, upvotes_received, downvotes_received, credits,
            about_seen, onboarding_acknowledged, onboarding_question_index,
            cooldown_until, cooldown_reason, cooldown_applied_by,
            downvote_streak, last_downvote_at, last_daily_tax_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        users,
    )
    return user_ids


def _cooldown_map(src: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    if not _table_exists(src, "cooldowns"):
        return {}
    data: dict[int, dict[str, Any]] = {}
    now = now_utc()
    for raw in src.execute("SELECT user_id, until_at, reason, applied_by FROM cooldowns"):
        row = dict(raw)
        until = parse_dt(row.get("until_at"))
        if until and until < now:
            continue
        data[int(row["user_id"])] = {
            "until_at": row.get("until_at"),
            "reason": row.get("reason") or "cooldown",
            "applied_by": row.get("applied_by"),
        }
    return data


def _downvote_map(src: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    if not _table_exists(src, "user_downvote_state"):
        return {}
    return {
        int(row["user_id"]): {
            "streak": row["streak"],
            "last_downvote_at": row["last_downvote_at"],
        }
        for row in src.execute("SELECT user_id, streak, last_downvote_at FROM user_downvote_state")
    }


def _tax_date_map(src: sqlite3.Connection) -> dict[int, str]:
    if not _table_exists(src, "credit_tax_runs"):
        return {}
    return {
        int(row["user_id"]): str(row["tax_date"])
        for row in src.execute("SELECT user_id, MAX(tax_date) AS tax_date FROM credit_tax_runs GROUP BY user_id")
        if row["tax_date"]
    }


def _migrate_media_hashes(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    if not _table_exists(src, "media_hashes"):
        return 0
    cols = _columns(src, "media_hashes")
    if {"hash", "first_seen_at", "latest_seen_at"}.issubset(cols):
        rows = src.execute(
            """
            SELECT hash, MIN(first_seen_at) AS first_seen_at
            FROM media_hashes
            WHERE hash IS NOT NULL AND hash != ''
            GROUP BY hash
            """
        ).fetchall()
    elif {"hash", "first_seen_at"}.issubset(cols):
        rows = src.execute(
            """
            SELECT hash, MIN(first_seen_at) AS first_seen_at
            FROM media_hashes
            WHERE hash IS NOT NULL AND hash != ''
            GROUP BY hash
            """
        ).fetchall()
    elif {"hash", "created_at"}.issubset(cols):
        rows = src.execute(
            """
            SELECT hash, MIN(created_at) AS first_seen_at
            FROM media_hashes
            WHERE hash IS NOT NULL AND hash != ''
            GROUP BY hash
            """
        ).fetchall()
    else:
        return 0
    dst.executemany(
        """
        INSERT OR REPLACE INTO media_hashes (hash, first_seen_at)
        VALUES (?, ?)
        """,
        [(row["hash"], row["first_seen_at"])
         for row in rows],
    )
    return len(rows)


def _migrate_blocks(src: sqlite3.Connection, dst: sqlite3.Connection, user_ids: set[int]) -> int:
    table = "user_blocks" if _table_exists(src, "user_blocks") else "blocks"
    if not _table_exists(src, table):
        return 0
    rows = []
    for row in src.execute(f'SELECT blocker_id, blocked_id, created_at FROM "{table}"'):
        blocker = int(row["blocker_id"])
        blocked = int(row["blocked_id"])
        if blocker in user_ids and blocked in user_ids:
            rows.append((blocker, blocked, str(row["created_at"])))
    dst.executemany(
        "INSERT OR IGNORE INTO user_blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
        rows,
    )
    return len(rows)


def _migrate_blocked_sticker_sets(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    if not _table_exists(src, "blocked_sticker_sets"):
        return 0
    rows = [
        (row["set_name"], row["blocked_by"], row["reason"], row["created_at"])
        for row in src.execute("SELECT set_name, blocked_by, reason, created_at FROM blocked_sticker_sets")
        if row["set_name"]
    ]
    dst.executemany(
        """
        INSERT OR REPLACE INTO blocked_sticker_sets (set_name, blocked_by, reason, created_at)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def _migrate_invites(src: sqlite3.Connection, dst: sqlite3.Connection, user_ids: set[int]) -> set[str]:
    if not _table_exists(src, "invites"):
        return set()
    rows = []
    invite_codes: set[str] = set()
    for row in src.execute("SELECT invite_code, inviter_id, uses, created_at FROM invites"):
        inviter = int(row["inviter_id"])
        code = str(row["invite_code"] or "")
        if inviter in user_ids and code:
            rows.append(
                (code, inviter, int(row["uses"] or 0), str(row["created_at"])))
            invite_codes.add(code)
    dst.executemany(
        "INSERT OR REPLACE INTO invites (invite_code, inviter_id, uses, created_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    return invite_codes


def _migrate_invite_redemptions(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    user_ids: set[int],
    invite_codes: set[str],
) -> int:
    if not _table_exists(src, "invite_redemptions"):
        return 0
    rows = []
    for row in src.execute("SELECT invite_code, invitee_id, created_at FROM invite_redemptions"):
        code = str(row["invite_code"] or "")
        invitee = int(row["invitee_id"])
        if code in invite_codes and invitee in user_ids:
            rows.append((code, invitee, str(row["created_at"])))
    dst.executemany(
        "INSERT OR IGNORE INTO invite_redemptions (invite_code, invitee_id, created_at) VALUES (?, ?, ?)",
        rows,
    )
    return len(rows)


def _migrate_credit_aggregates(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    user_ids: set[int],
) -> dict[str, int]:
    if not _table_exists(src, "credit_transactions"):
        return {
            "credit_daily_earnings": 0,
            "credit_global_daily": 0,
        }

    daily_earnings: dict[tuple[int, str, str], float] = defaultdict(float)
    global_daily: dict[str, float] = defaultdict(float)
    for row in src.execute("SELECT user_id, amount, reason, created_at FROM credit_transactions"):
        user_id = int(row["user_id"])
        if user_id not in user_ids:
            continue
        day = str(row["created_at"])[:10]
        if len(day) != 10:
            continue
        amount = round_credits(float(row["amount"]))
        reason = str(row["reason"] or "unknown")
        global_daily[day] += amount
        if amount > 0:
            daily_earnings[(user_id, day, reason)] += amount

    dst.executemany(
        "INSERT OR REPLACE INTO credit_global_daily (day, net_amount) VALUES (?, ?)",
        [(day, round_credits(amount)) for day, amount in global_daily.items()],
    )
    dst.executemany(
        """
        INSERT OR REPLACE INTO credit_daily_earnings (user_id, day, reason, positive_amount)
        VALUES (?, ?, ?, ?)
        """,
        [
            (user_id, day, reason, round_credits(amount))
            for (user_id, day, reason), amount in daily_earnings.items()
        ],
    )
    return {
        "credit_daily_earnings": len(daily_earnings),
        "credit_global_daily": len(global_daily),
    }
