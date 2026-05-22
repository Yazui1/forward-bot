from __future__ import annotations

import aiosqlite


async def migrate_if_needed(db: aiosqlite.Connection) -> None:
    columns = await _table_columns(db, "media_hashes")
    if not columns or "message_id" not in columns:
        return

    await db.execute("ALTER TABLE media_hashes RENAME TO media_hashes_old")
    await db.execute(
        """
        CREATE TABLE media_hashes (
            hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        """
        INSERT INTO media_hashes (hash, created_at)
        SELECT hash, COALESCE(created_at, CURRENT_TIMESTAMP)
        FROM media_hashes_old
        WHERE hash IS NOT NULL AND hash != ''
        """
    )
    await db.execute("DROP TABLE media_hashes_old")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_hashes_created_at ON media_hashes(created_at)"
    )
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (name) VALUES ('media_hashes_drop_message_id')"
    )
    await db.commit()


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    return {str(row["name"]) for row in rows}
