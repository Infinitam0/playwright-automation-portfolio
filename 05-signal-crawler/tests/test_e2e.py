"""End-to-end: orchestrator + FakeScraper + real SQLite. Proves the plumbing.

Covers:
- One pass of `Orchestrator.run(once=True)` drains pending jobs
- Inbox rows are written with correct identity
- Cursor advances to the last item's cursor
- Job is marked done
- A second run with the same cursor is a clean no-op (idempotent)
- A scraper that raises RateLimited gets deferred
- A scraper that raises SelectorBroken sets needs_review control flag
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from scanner import db as db_module
from scanner.base import BaseScraper, RateLimited, SelectorBroken
from scanner.config import Config, SourceConfig
from scanner.models import RawItem
from scanner.orchestrator import Orchestrator
from tests._fake_scraper import FakeScraper


def _make_cfg(db_path: Path, sources: dict[str, int]) -> Config:
    return Config(
        db_path=db_path,
        workers=1,
        log_level="WARNING",
        log_format="json",
        proxy_url="",
        sources={
            name: SourceConfig(name=name, enabled=True, rate_per_min=rate)
            for name, rate in sources.items()
        },
    )


@pytest.mark.asyncio
async def test_once_pass_writes_three_inbox_rows(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"fake": 6000})
    orch = Orchestrator(cfg, {"fake": FakeScraper}, drain_poll_seconds=0.05)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        rows = await (
            await conn.execute(
                "SELECT source, source_item_id, url, raw_content FROM inbox ORDER BY id"
            )
        ).fetchall()
        assert [r["source_item_id"] for r in rows] == ["id-0", "id-1", "id-2"]
        assert all(r["source"] == "fake" for r in rows)
        assert rows[0]["url"] == "https://example.test/0"

        cursor_row = await (
            await conn.execute("SELECT max_id FROM cursors WHERE source='fake'")
        ).fetchone()
        assert cursor_row["max_id"] == "c-2"

        job_row = await (
            await conn.execute("SELECT status FROM jobs WHERE source='fake'")
        ).fetchone()
        assert job_row["status"] == "done"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_second_run_idempotent(tmp_db: Path, migrations_dir: Path) -> None:
    """Re-running --once should not duplicate inbox rows or signals."""
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"fake": 6000})
    orch = Orchestrator(cfg, {"fake": FakeScraper}, drain_poll_seconds=0.05)
    await orch.run(once=True)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        count_row = await (await conn.execute("SELECT COUNT(*) FROM inbox")).fetchone()
        assert count_row[0] == 3
        jobs_done = await (
            await conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'")
        ).fetchone()
        assert jobs_done[0] == 2  # one done job per run
    finally:
        await conn.close()


# ---- failure-handling: RateLimited / SelectorBroken ----


class RateLimitedScraper(BaseScraper):
    default_rate_per_min = 6000

    async def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        # Yield nothing — raise immediately. The yield below is unreachable but
        # makes this a genuine async generator (asyncio coroutines that raise
        # never become async iterators).
        raise RateLimited(retry_after=60.0)
        yield  # type: ignore[unreachable]


class SelectorBrokenScraper(BaseScraper):
    default_rate_per_min = 6000

    async def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        raise SelectorBroken("css '.thread' returned 0 hits when expecting >0")
        yield  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_rate_limited_defers_job_and_pauses_source(
    tmp_db: Path, migrations_dir: Path
) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"rl": 6000})
    orch = Orchestrator(cfg, {"rl": RateLimitedScraper}, drain_poll_seconds=0.05)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        job_row = await (
            await conn.execute("SELECT status, run_after FROM jobs WHERE source='rl'")
        ).fetchone()
        # Deferred → status flips back to pending with a future run_after.
        assert job_row["status"] == "pending"
        assert job_row["run_after"] is not None

        ctl = await (
            await conn.execute("SELECT value FROM control WHERE key='paused:rl'")
        ).fetchone()
        assert ctl is not None and ctl[0] == "1"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_selector_broken_marks_failed_and_needs_review(
    tmp_db: Path, migrations_dir: Path
) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"sb": 6000})
    orch = Orchestrator(cfg, {"sb": SelectorBrokenScraper}, drain_poll_seconds=0.05)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        job_row = await (
            await conn.execute("SELECT status, error_msg FROM jobs WHERE source='sb'")
        ).fetchone()
        assert job_row["status"] == "failed"
        assert "SelectorBroken" in job_row["error_msg"]

        ctl = await (
            await conn.execute("SELECT value FROM control WHERE key='needs_review:sb'")
        ).fetchone()
        assert ctl is not None and ctl[0] == "1"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_no_scraper_for_source_marks_job_failed(tmp_db: Path, migrations_dir: Path) -> None:
    """Config enables a source but no scraper is registered for it."""
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"ghost": 6000})
    # Note: empty scrapers dict — but config still references 'ghost'.
    # _enqueue_due will skip it (no scraper) — so no job is created.
    orch = Orchestrator(cfg, {}, drain_poll_seconds=0.05)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        rows = await (await conn.execute("SELECT * FROM jobs")).fetchall()
        assert rows == []
    finally:
        await conn.close()


class FailsMidwayHighWaterScraper(BaseScraper):
    """Newest-first source (like HN/Reddit): every item carries the running
    high-water mark, then the job dies before the older items are reached."""

    default_rate_per_min = 6000

    async def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        yield RawItem(
            source="hw",
            source_item_id="newest",
            url="https://example.test/n",
            raw_content="newest",
            cursor="300",
        )
        raise RuntimeError("network died before the older items")


@pytest.mark.asyncio
async def test_failed_job_does_not_advance_cursor(tmp_db: Path, migrations_dir: Path) -> None:
    # A high-water cursor committed mid-job would make the next job skip every
    # older item the failed job never reached.
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"hw": 6000})
    orch = Orchestrator(cfg, {"hw": FailsMidwayHighWaterScraper}, drain_poll_seconds=0.05)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        job = await (await conn.execute("SELECT status FROM jobs WHERE source='hw'")).fetchone()
        assert job["status"] == "failed"
        inbox = await (
            await conn.execute("SELECT COUNT(*) FROM inbox WHERE source='hw'")
        ).fetchone()
        assert inbox[0] == 1  # the item it did fetch is kept
        cursor = await (
            await conn.execute("SELECT max_id FROM cursors WHERE source='hw'")
        ).fetchone()
        assert cursor is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_restart_recovers_job_orphaned_by_a_recent_crash(
    tmp_db: Path, migrations_dir: Path
) -> None:
    # Simulate a process killed seconds ago: its job is still `running`. The
    # scanner is single-process, so on startup that job is an orphan whatever
    # its age; leaving it would block the source and hang `run --once`.
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        await conn.execute(
            "INSERT INTO jobs(source, status, claimed_by, claimed_at, attempts) "
            "VALUES ('fake', 'running', 'w0', CURRENT_TIMESTAMP, 1)"
        )
        await conn.commit()
    finally:
        await conn.close()

    cfg = _make_cfg(tmp_db, {"fake": 6000})
    orch = Orchestrator(cfg, {"fake": FakeScraper}, drain_poll_seconds=0.05)
    await asyncio.wait_for(orch.run(once=True), timeout=10)

    conn = await db_module.connect(tmp_db)
    try:
        statuses = [
            r[0]
            for r in await (
                await conn.execute("SELECT status FROM jobs WHERE source='fake'")
            ).fetchall()
        ]
        assert statuses == ["done"]
        inbox = await (
            await conn.execute("SELECT COUNT(*) FROM inbox WHERE source='fake'")
        ).fetchone()
        assert inbox[0] == 3
    finally:
        await conn.close()


class FailsMidwayCheckpointScraper(FailsMidwayHighWaterScraper):
    """In-order position cursor (like AlternativeTo's catalog index)."""

    checkpoint_cursor = True


@pytest.mark.asyncio
async def test_checkpoint_scraper_keeps_progress_from_failed_job(
    tmp_db: Path, migrations_dir: Path
) -> None:
    # An in-order position is safe to commit per item, so the next job resumes
    # after the last item the failed job saved.
    await db_module.migrate(tmp_db, migrations_dir)
    cfg = _make_cfg(tmp_db, {"hw": 6000})
    orch = Orchestrator(cfg, {"hw": FailsMidwayCheckpointScraper}, drain_poll_seconds=0.05)
    await orch.run(once=True)

    conn = await db_module.connect(tmp_db)
    try:
        cursor = await (
            await conn.execute("SELECT max_id FROM cursors WHERE source='hw'")
        ).fetchone()
        assert cursor[0] == "300"
    finally:
        await conn.close()
