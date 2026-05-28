from __future__ import annotations

import aiosqlite


async def migrate_if_needed(db: aiosqlite.Connection) -> None:
    columns = [row["name"] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()]
    if "filter_duplicates" in columns:
        return
    await db.execute(
        "ALTER TABLE users ADD COLUMN filter_duplicates INTEGER NOT NULL DEFAULT 0"
    )
