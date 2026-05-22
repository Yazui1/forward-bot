Database migrations live here.

`legacy_secretlounge.py` migrates the old Secret Lounge SQLite schema into
the current schema during `init_schema()`. Future persistent schema upgrades
should be added here and called from `forward_bot/db/schema.py`.
