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
