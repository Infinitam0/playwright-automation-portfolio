"""Async SQLite layer.

Owns the connection factory, durability pragmas, and the migration runner.
Higher-level call sites (jobqueue, orchestrator, extractor) get a ready
connection via `connect()` and run raw SQL. No ORM.

Durability config:
    journal_mode = WAL          # survives hard power-cut
    synchronous  = NORMAL       # WAL-safe; ~5-10% faster than FULL
    busy_timeout = 5000         # 5s retry before SQLITE_BUSY
    foreign_keys = ON           # enforces inbox -> signals cascade
"""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite

from scanner.logging import get_logger

log = get_logger(__name__)

# Pragmas applied to every connection. WAL is persistent across opens; the rest
# must be re-applied per connection because SQLite scopes them per-connection.
_INIT_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA foreign_keys = ON;",
)


async def connect(db_path: str | Path) -> aiosqlite.Connection:
    """Open a connection with durability pragmas applied. Caller owns close()."""
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    for pragma in _INIT_PRAGMAS:
        await conn.execute(pragma)
    await conn.commit()
    return conn


_MIGRATION_FILENAME = re.compile(r"^(\d{4})_[\w-]+\.sql$")


async def migrate(db_path: str | Path, migrations_dir: str | Path = "migrations") -> list[int]:
    """Apply any not-yet-applied migrations in numeric order.

    Returns the list of versions newly applied. Idempotent: running twice with
    no new migration files is a no-op.

    Conventions:
        - migrations/0001_init.sql, 0002_indexes.sql, ...
        - filename must match \\d{4}_[\\w-]+\\.sql
        - applied versions are recorded in the _migrations table
        - each migration runs in its own transaction
    """
    migrations_path = Path(migrations_dir)
    if not migrations_path.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {migrations_path}")

    files = []
    for f in sorted(migrations_path.iterdir()):
        if not f.is_file():
            continue
        m = _MIGRATION_FILENAME.match(f.name)
        if not m:
            continue
        files.append((int(m.group(1)), f))

    conn = await connect(db_path)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version    INTEGER PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()

        cursor = await conn.execute("SELECT version FROM _migrations")
        applied = {row[0] for row in await cursor.fetchall()}
        await cursor.close()

        newly_applied: list[int] = []
        for version, path in files:
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            log.info("migration.apply", version=version, file=path.name)
            try:
                await conn.executescript(sql)
                await conn.execute("INSERT INTO _migrations(version) VALUES (?)", (version,))
                await conn.commit()
            except Exception:
                await conn.rollback()
                log.error("migration.failed", version=version, file=path.name)
                raise
            newly_applied.append(version)

        return newly_applied
    finally:
        await conn.close()


async def journal_mode(db_path: str | Path) -> str:
    """Return current journal_mode for assertion in tests."""
    conn = await connect(db_path)
    try:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else ""
    finally:
        await conn.close()


async def list_tables(db_path: str | Path) -> list[str]:
    """Return sorted list of user tables. Excludes sqlite_* internals."""
    conn = await connect(db_path)
    try:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [r[0] for r in rows]
    finally:
        await conn.close()
