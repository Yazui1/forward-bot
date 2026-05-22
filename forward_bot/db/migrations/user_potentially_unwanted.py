from __future__ import annotations

import aiosqlite


async def migrate_if_needed(db: aiosqlite.Connection) -> None:
    columns = await _table_columns(db, "users")
    if not columns or "hide_potentially_unwanted" in columns:
        return
    await db.execute(
        "ALTER TABLE users ADD COLUMN hide_potentially_unwanted INTEGER NOT NULL DEFAULT 0"
    )
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (name) VALUES ('user_potentially_unwanted')"
    )
    await db.commit()


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    return {str(row["name"]) for row in rows}
