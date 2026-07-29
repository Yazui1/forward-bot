from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from forward_bot.db.schema import init_schema
from forward_bot.utils import iso, now_utc, parse_dt, round_credits, seconds_left, today_key


@dataclass(slots=True)
class User:
    telegram_id: int
    username: str | None
    has_started: bool
    created_at: str
    last_activity: str | None
    is_banned: bool
    is_moderator: bool
    is_admin: bool
    confirmation_enabled: bool
    votes_enabled: bool
    vote_buttons_enabled: bool
    hide_potentially_unwanted: bool
    filter_duplicates: bool
    preserve_forwards: bool
    fights_enabled: bool
    sign_enabled: bool
    tripcode_enabled: bool
    tripcode_name: str | None
    tripcode_hash: str | None
    warning_count: int
    upvotes_received: int
    downvotes_received: int
    credits: float
    about_seen: bool
    onboarding_acknowledged: bool
    onboarding_question_index: int
    cooldown_until: str | None
    cooldown_reason: str | None
    cooldown_applied_by: int | None
    downvote_streak: int
    last_downvote_at: str | None
    last_daily_tax_date: str | None

    @property
    def active_cooldown_seconds(self) -> int:
        return seconds_left(self.cooldown_until)

    @property
    def is_mod_or_admin(self) -> bool:
        return self.is_admin or self.is_moderator


class Repository:
    def __init__(self, path: str | Path, about_text: str = ""):
        self.path = Path(path)
        init_schema(self.path)
        self.about_text = self._load_about_text(about_text)
        self._users: dict[int, User] = {}
        self._blocks: set[tuple[int, int]] = set()
        self._load_cache()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def ensure_user(
        self,
        telegram_id: int,
        username: str | None,
        starting_balance: float,
        admin_ids: Iterable[int] = (),
        onboarding_acknowledged: bool = False,
    ) -> tuple[User, bool]:
        admin = int(telegram_id in set(int(x) for x in admin_ids))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            created = row is None
            if created:
                conn.execute(
                    """
                    INSERT INTO users (
                        telegram_id, username, created_at, credits, is_admin, onboarding_acknowledged
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (telegram_id, username, iso(),
                     round_credits(starting_balance), admin, int(onboarding_acknowledged or bool(admin))),
                )
            else:
                conn.execute(
                    "UPDATE users SET username=?, is_admin=? WHERE telegram_id=?",
                    (username, admin, telegram_id),
                )
            conn.commit()
        self._refresh_user(telegram_id)
        # type: ignore[return-value]
        return self.get_user(telegram_id), created

    def get_user(self, telegram_id: int | None) -> User | None:
        if telegram_id is None:
            return None
        return self._users.get(int(telegram_id))

    def list_users(self) -> list[User]:
        return sorted(self._users.values(), key=lambda user: user.created_at)

    def sync_admin_ids(self, admin_ids: Iterable[int]) -> None:
        ids = {int(x) for x in admin_ids}
        with self.connect() as conn:
            conn.execute("UPDATE users SET is_admin=0")
            for user_id in ids:
                row = conn.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id=?", (user_id,)).fetchone()
                if row:
                    conn.execute(
                        "UPDATE users SET is_admin=1 WHERE telegram_id=?", (user_id,))
                else:
                    conn.execute(
                        "INSERT INTO users (telegram_id, created_at, is_admin, credits, onboarding_acknowledged) VALUES (?, ?, 1, 0, 1)",
                        (user_id, iso()),
                    )
            conn.commit()
        self._load_cache()

    def set_started(self, user_id: int, started: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET has_started=? WHERE telegram_id=?", (int(started), user_id))
            conn.commit()
        self._refresh_user(user_id)

    def mark_left(self, user_id: int) -> None:
        self.set_started(user_id, False)

    def touch_activity(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET last_activity=? WHERE telegram_id=?", (iso(), user_id))
            conn.commit()
        self._refresh_user(user_id)

    def set_about_seen(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET about_seen=1 WHERE telegram_id=?", (user_id,))
            conn.commit()
        self._refresh_user(user_id)

    def set_onboarding_progress(self, user_id: int, *, acknowledged: bool, question_index: int | None = None) -> User | None:
        updates = ["onboarding_acknowledged=?"]
        values: list[Any] = [int(acknowledged)]
        if question_index is not None:
            updates.append("onboarding_question_index=?")
            values.append(max(0, int(question_index)))
        values.append(user_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE telegram_id=?",
                values,
            )
            conn.commit()
        self._refresh_user(user_id)
        return self.get_user(user_id)

    def set_preference(self, user_id: int, column: str, value: bool) -> User:
        allowed = {
            "confirmation_enabled",
            "votes_enabled",
            "vote_buttons_enabled",
            "hide_potentially_unwanted",
            "filter_duplicates",
            "preserve_forwards",
            "fights_enabled",
            "sign_enabled",
            "tripcode_enabled",
        }
        if column not in allowed:
            raise ValueError(column)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE users SET {column}=? WHERE telegram_id=?", (int(value), user_id))
            conn.commit()
        self._refresh_user(user_id)
        return self.get_user(user_id)  # type: ignore[return-value]

    def set_tripcode(self, user_id: int, name: str | None, code: str | None, enabled: bool) -> User:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET tripcode_name=?, tripcode_hash=?, tripcode_enabled=? WHERE telegram_id=?",
                (name, code, int(enabled), user_id),
            )
            conn.commit()
        self._refresh_user(user_id)
        return self.get_user(user_id)  # type: ignore[return-value]

    def set_role(self, user_id: int, *, moderator: bool | None = None, banned: bool | None = None) -> User | None:
        updates: list[str] = []
        values: list[Any] = []
        if moderator is not None:
            updates.append("is_moderator=?")
            values.append(int(moderator))
        if banned is not None:
            updates.append("is_banned=?")
            values.append(int(banned))
            if banned:
                updates.append("has_started=?")
                values.append(0)
        if not updates:
            return self.get_user(user_id)
        values.append(user_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE telegram_id=?", values)
            conn.commit()
        self._refresh_user(user_id)
        return self.get_user(user_id)

    def increment_warning(self, user_id: int) -> int:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET warning_count=warning_count+1 WHERE telegram_id=?", (user_id,))
            row = conn.execute(
                "SELECT warning_count FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            conn.commit()
        self._refresh_user(user_id)
        return int(row["warning_count"]) if row else 0

    def find_by_username(self, username: str) -> User | None:
        username = username.lstrip("@").lower()
        matches = [user for user in self._users.values() if (
            user.username or "").lower() == username]
        return max(matches, key=lambda user: user.last_activity or "") if matches else None

    def find_by_tripcode(self, name: str, code: str) -> User | None:
        for user in self._users.values():
            if user.tripcode_name == name and user.tripcode_hash == code:
                return user
        return None

    def eligible_recipients(self, sender_id: int | None, *, include_sender: bool = False) -> list[User]:
        users = sorted(
            (user for user in self._users.values()
             if user.has_started and not user.is_banned),
            key=lambda user: user.last_activity or "",
            reverse=True,
        )
        if sender_id is None:
            return users
        filtered: list[User] = []
        for user in users:
            if not include_sender and user.telegram_id == sender_id:
                continue
            if not user.is_mod_or_admin and self.is_blocked(user.telegram_id, sender_id):
                continue
            filtered.append(user)
        return filtered

    def add_block(self, blocker_id: int, blocked_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
                (blocker_id, blocked_id, iso()),
            )
            conn.commit()
        self._blocks.add((blocker_id, blocked_id))

    def remove_latest_block(self, blocker_id: int) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT blocked_id FROM user_blocks WHERE blocker_id=? ORDER BY created_at DESC LIMIT 1",
                (blocker_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "DELETE FROM user_blocks WHERE blocker_id=? AND blocked_id=?",
                (blocker_id, row["blocked_id"]),
            )
            conn.commit()
            blocked_id = int(row["blocked_id"])
            self._blocks.discard((blocker_id, blocked_id))
            return blocked_id

    def is_blocked(self, blocker_id: int, blocked_id: int) -> bool:
        return (blocker_id, blocked_id) in self._blocks

    def set_cooldown(self, user_id: int, seconds: int, reason: str, applied_by: int | None = None, *, stack: bool = True) -> User | None:
        user = self.get_user(user_id)
        if not user:
            return None
        base = now_utc()
        current = parse_dt(user.cooldown_until)
        if stack and current and current > base:
            base = current
        until = base + timedelta(seconds=max(1, int(seconds)))
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET cooldown_until=?, cooldown_reason=?, cooldown_applied_by=? WHERE telegram_id=?",
                (iso(until), reason or "cooldown", applied_by, user_id),
            )
            conn.commit()
        self._refresh_user(user_id)
        return self.get_user(user_id)

    def clear_cooldown(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET cooldown_until=NULL, cooldown_reason=NULL, cooldown_applied_by=NULL WHERE telegram_id=?",
                (user_id,),
            )
            conn.commit()
        self._refresh_user(user_id)

    def get_active_cooldown(self, user_id: int) -> dict[str, Any] | None:
        user = self.get_user(user_id)
        if not user or user.active_cooldown_seconds <= 0:
            return None
        return {
            "user_id": user_id,
            "until_at": user.cooldown_until,
            "reason": user.cooldown_reason or "cooldown",
            "applied_by": user.cooldown_applied_by,
        }

    def list_active_cooldowns(self) -> list[tuple[User, int, str]]:
        users = []
        for user in self.list_users():
            left = user.active_cooldown_seconds
            if left > 0:
                users.append((user, left, user.cooldown_reason or "cooldown"))
        return users

    def apply_credit_change(
        self,
        user_id: int,
        amount: float,
        reason: str,
        *,
        daily_caps: dict[str, float] | None = None,
    ) -> tuple[float, User | None]:
        amount = round_credits(amount)
        if amount > 0 and daily_caps is not None:
            cap = daily_caps.get(reason)
            if cap is not None and cap >= 0:
                earned = self.positive_credits_today(user_id, reason)
                amount = round_credits(min(amount, max(0.0, cap - earned)))
        if amount == 0:
            return 0.0, self.get_user(user_id)
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET credits=ROUND(credits + ?, 2) WHERE telegram_id=?",
                (amount, user_id),
            )
            self._record_credit_delta(conn, user_id, amount, reason)
            conn.commit()
        self._refresh_user(user_id)
        return amount, self.get_user(user_id)

    def transfer_credits(self, sender_id: int, target_id: int, amount: float, reason: str = "transfer") -> tuple[User | None, User | None]:
        amount = round_credits(amount)
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET credits=ROUND(credits - ?, 2) WHERE telegram_id=?", (amount, sender_id))
            conn.execute(
                "UPDATE users SET credits=ROUND(credits + ?, 2) WHERE telegram_id=?", (amount, target_id))
            self._record_credit_delta(conn, sender_id, -amount, reason)
            self._record_credit_delta(conn, target_id, amount, reason)
            conn.commit()
        self._refresh_user(sender_id)
        self._refresh_user(target_id)
        return self.get_user(sender_id), self.get_user(target_id)

    def increment_vote_stat(self, user_id: int, up: bool) -> None:
        column = "upvotes_received" if up else "downvotes_received"
        with self.connect() as conn:
            conn.execute(
                f"UPDATE users SET {column}={column}+1 WHERE telegram_id=?", (user_id,))
            conn.commit()
        self._refresh_user(user_id)

    def positive_credits_today(self, user_id: int, reason: str) -> float:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT positive_amount FROM credit_daily_earnings WHERE user_id=? AND day=? AND reason=?",
                (user_id, today_key(), reason),
            ).fetchone()
        return float(row["positive_amount"]) if row else 0.0

    def _record_credit_delta(self, conn: sqlite3.Connection, user_id: int, amount: float, reason: str) -> None:
        day = today_key()
        conn.execute(
            """
            INSERT INTO credit_daily_net (user_id, day, net_amount) VALUES (?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET net_amount=ROUND(net_amount + excluded.net_amount, 2)
            """,
            (user_id, day, amount),
        )
        conn.execute(
            """
            INSERT INTO credit_global_daily (day, net_amount) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET net_amount=ROUND(net_amount + excluded.net_amount, 2)
            """,
            (day, amount),
        )
        if amount > 0:
            conn.execute(
                """
                INSERT INTO credit_daily_earnings (user_id, day, reason, positive_amount) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, day, reason) DO UPDATE SET positive_amount=ROUND(positive_amount + excluded.positive_amount, 2)
                """,
                (user_id, day, reason, amount),
            )

    def top_current_credits(self, limit: int = 10) -> list[User]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY credits DESC, last_activity DESC LIMIT ?",
                (limit,),
            ).fetchall()
        users = [_row_to_user(r) for r in rows]
        for user in users:
            self._users[user.telegram_id] = user
        return users

    def top_daily_earners(self, limit: int = 10) -> list[tuple[User, float]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT u.*, SUM(e.positive_amount) AS earned
                FROM credit_daily_earnings e
                JOIN users u ON u.telegram_id=e.user_id
                WHERE e.day=?
                GROUP BY e.user_id
                ORDER BY earned DESC
                LIMIT ?
                """,
                (today_key(), limit),
            ).fetchall()
        return [(_row_to_user(r), float(r["earned"])) for r in rows]

    def net_issuance_since_days(self, days: int) -> float:
        cutoff = (now_utc().date() -
                  timedelta(days=max(0, days - 1))).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT SUM(net_amount) AS net FROM credit_global_daily WHERE day>=?",
                (cutoff,),
            ).fetchone()
        return float(row["net"] or 0.0)

    def credit_values(self, *, started_only: bool = True) -> list[float]:
        query = "SELECT credits FROM users WHERE is_banned=0"
        if started_only:
            query += " AND has_started=1"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [float(r["credits"]) for r in rows]

    def credit_percentile_cutoff(self, top_fraction: float) -> float:
        if top_fraction <= 0:
            return 0.0
        values = sorted(self.credit_values(started_only=True), reverse=True)
        if not values:
            return 0.0
        frac = top_fraction / 100.0 if top_fraction > 1 else top_fraction
        frac = max(0.0, min(1.0, frac))
        index = max(0, min(len(values) - 1, int(len(values) * frac) - 1))
        return float(values[index])

    def get_downvote_state(self, user_id: int) -> tuple[int, str | None]:
        user = self.get_user(user_id)
        if not user:
            return 0, None
        return user.downvote_streak, user.last_downvote_at

    def set_downvote_state(self, user_id: int, streak: int, at: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET downvote_streak=?, last_downvote_at=? WHERE telegram_id=?",
                (max(0, int(streak)), at or iso(), user_id),
            )
            conn.commit()
        self._refresh_user(user_id)

    def apply_daily_tax_once(self, user: User, rate: float, reason: str = "daily_tax") -> tuple[float, User | None]:
        day = today_key()
        if user.last_daily_tax_date == day or user.is_banned:
            return 0.0, user
        amount = -round_credits(max(0.0, user.credits * rate))
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET last_daily_tax_date=?, credits=ROUND(credits + ?, 2) WHERE telegram_id=?",
                (day, amount, user.telegram_id),
            )
            if amount:
                self._record_credit_delta(
                    conn, user.telegram_id, amount, reason)
            conn.commit()
        self._refresh_user(user.telegram_id)
        return amount, self.get_user(user.telegram_id)

    def get_media_hash(self, digest: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_hashes WHERE hash=?", (digest,)).fetchone()
        return dict(row) if row else None

    def upsert_media_hash(self, digest: str, *, first_seen_at: str | None = None) -> dict[str, Any]:
        now = iso()
        first = first_seen_at or now
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO media_hashes (hash, first_seen_at, latest_seen_at) VALUES (?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET latest_seen_at=excluded.latest_seen_at
                """,
                (digest, first, now),
            )
            row = conn.execute(
                "SELECT * FROM media_hashes WHERE hash=?", (digest,)).fetchone()
            conn.commit()
        return dict(row)

    def get_blocked_sticker_set(self, set_name: str | None) -> dict[str, Any] | None:
        if not set_name:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM blocked_sticker_sets WHERE set_name=?", (set_name,)).fetchone()
        return dict(row) if row else None

    def block_sticker_set(self, set_name: str, blocked_by: int, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO blocked_sticker_sets (set_name, blocked_by, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(set_name) DO UPDATE SET blocked_by=excluded.blocked_by, reason=excluded.reason, created_at=excluded.created_at
                """,
                (set_name, blocked_by, reason, iso()),
            )
            conn.commit()

    def get_invite_for_user(self, user_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT invite_code FROM invites WHERE inviter_id=? ORDER BY created_at LIMIT 1", (user_id,)).fetchone()
        return str(row["invite_code"]) if row else None

    def create_invite(self, user_id: int, code: str) -> str:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO invites (invite_code, inviter_id, uses, created_at) VALUES (?, ?, 0, ?)",
                (code, user_id, iso()),
            )
            conn.commit()
        return code

    def get_invite(self, code: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM invites WHERE invite_code=?", (code,)).fetchone()
        return dict(row) if row else None

    def redeem_invite(self, code: str, invitee_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            invite = conn.execute(
                "SELECT * FROM invites WHERE invite_code=?", (code,)).fetchone()
            if not invite or int(invite["inviter_id"]) == invitee_id:
                return None
            existing = conn.execute(
                "SELECT 1 FROM invite_redemptions WHERE invitee_id=?",
                (invitee_id,),
            ).fetchone()
            if existing:
                return None
            conn.execute(
                "INSERT INTO invite_redemptions (invite_code, invitee_id, created_at) VALUES (?, ?, ?)",
                (code, invitee_id, iso()),
            )
            conn.execute(
                "UPDATE invites SET uses=uses+1 WHERE invite_code=?", (code,))
            conn.commit()
            return dict(invite)

    def get_about(self) -> str:
        return self.about_text

    def set_about(self, text: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_state (state_key, state_value)
                VALUES ('about', ?)
                ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value
                """,
                (text,),
            )
            conn.commit()
        self.about_text = text

    def list_ack_rules(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM bot_state WHERE state_key='ack_rules'",
            ).fetchone()
        if row is None:
            return []
        try:
            raw = json.loads(str(row["state_value"]))
        except json.JSONDecodeError:
            return []
        if not isinstance(raw, list):
            return []
        rules: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            question = item.get("question")
            answer = item.get("answer")
            if isinstance(question, str) and isinstance(answer, str) and question and answer:
                rules.append({"question": question, "answer": answer})
        return rules

    def add_ack_rule(self, question: str, answer: str) -> list[dict[str, str]]:
        rules = self.list_ack_rules()
        rules.append({"question": question, "answer": answer})
        self._save_ack_rules(rules)
        return rules

    def drop_last_ack_rule(self) -> dict[str, str] | None:
        rules = self.list_ack_rules()
        if not rules:
            return None
        dropped = rules.pop()
        self._save_ack_rules(rules)
        return dropped

    def _save_ack_rules(self, rules: list[dict[str, str]]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_state (state_key, state_value)
                VALUES ('ack_rules', ?)
                ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value
                """,
                (json.dumps(rules, ensure_ascii=False),),
            )
            conn.commit()

    def _load_cache(self) -> None:
        with self.connect() as conn:
            self._users = {
                int(row["telegram_id"]): _row_to_user(row)
                for row in conn.execute("SELECT * FROM users").fetchall()
            }
            self._blocks = {
                (int(row["blocker_id"]), int(row["blocked_id"]))
                for row in conn.execute("SELECT blocker_id, blocked_id FROM user_blocks").fetchall()
            }

    def _load_about_text(self, default: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM bot_state WHERE state_key='about'",
            ).fetchone()
            if row is not None:
                return str(row["state_value"])
            conn.execute(
                "INSERT INTO bot_state (state_key, state_value) VALUES ('about', ?)",
                (default,),
            )
            conn.commit()
        return default

    def _refresh_user(self, user_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        if row:
            self._users[int(user_id)] = _row_to_user(row)
        else:
            self._users.pop(int(user_id), None)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=int(row["telegram_id"]),
        username=row["username"],
        has_started=bool(row["has_started"]),
        created_at=row["created_at"],
        last_activity=row["last_activity"],
        is_banned=bool(row["is_banned"]),
        is_moderator=bool(row["is_moderator"]),
        is_admin=bool(row["is_admin"]),
        confirmation_enabled=bool(row["confirmation_enabled"]),
        votes_enabled=bool(row["votes_enabled"]),
        vote_buttons_enabled=bool(row["vote_buttons_enabled"]),
        hide_potentially_unwanted=bool(row["hide_potentially_unwanted"]),
        filter_duplicates=bool(row["filter_duplicates"]),
        preserve_forwards=bool(row["preserve_forwards"]),
        fights_enabled=bool(row["fights_enabled"]),
        sign_enabled=bool(row["sign_enabled"]),
        tripcode_enabled=bool(row["tripcode_enabled"]),
        tripcode_name=row["tripcode_name"],
        tripcode_hash=row["tripcode_hash"],
        warning_count=int(row["warning_count"]),
        upvotes_received=int(row["upvotes_received"]),
        downvotes_received=int(row["downvotes_received"]),
        credits=float(row["credits"]),
        about_seen=bool(row["about_seen"]),
        onboarding_acknowledged=bool(row["onboarding_acknowledged"]),
        onboarding_question_index=int(row["onboarding_question_index"]),
        cooldown_until=row["cooldown_until"],
        cooldown_reason=row["cooldown_reason"],
        cooldown_applied_by=row["cooldown_applied_by"],
        downvote_streak=int(row["downvote_streak"]),
        last_downvote_at=row["last_downvote_at"],
        last_daily_tax_date=row["last_daily_tax_date"],
    )
