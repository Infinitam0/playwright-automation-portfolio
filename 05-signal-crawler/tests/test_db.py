"""Database + migrations smoke tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scanner import db as db_module

EXPECTED_TABLES = {
    "_migrations",
    "apps",
    "control",
    "cursors",
    "inbox",
    "inbox_parse_error",
    "jobs",
    "runs",
    "signals",
    "sources",
}


@pytest.mark.asyncio
async def test_migrate_creates_all_tables(tmp_db: Path, migrations_dir: Path) -> None:
    applied = await db_module.migrate(tmp_db, migrations_dir)
    assert applied == [1, 2], f"expected to apply migrations [1, 2], got {applied}"

    tables = set(await db_module.list_tables(tmp_db))
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


@pytest.mark.asyncio
async def test_migrate_is_idempotent(tmp_db: Path, migrations_dir: Path) -> None:
    first = await db_module.migrate(tmp_db, migrations_dir)
    second = await db_module.migrate(tmp_db, migrations_dir)
    assert first == [1, 2]
    assert second == []


@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    mode = await db_module.journal_mode(tmp_db)
    assert mode.lower() == "wal", f"expected WAL journal mode, got {mode!r}"


@pytest.mark.asyncio
async def test_jobs_status_check_constraint(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        await conn.execute("INSERT INTO sources(name, rate_per_min) VALUES ('test', 10)")
        await conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute("INSERT INTO jobs(source, status) VALUES ('test', 'bogus')")
            await conn.commit()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_inbox_unique_source_item(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        await conn.execute(
            "INSERT INTO inbox(source, source_item_id, raw_content, hash) "
            "VALUES ('reddit', 't3_abc', 'body', 'h1')"
        )
        await conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO inbox(source, source_item_id, raw_content, hash) "
                "VALUES ('reddit', 't3_abc', 'other body', 'h2')"
            )
            await conn.commit()
    finally:
        await conn.close()
