"""JobQueue tests against a real (per-test) SQLite database."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanner import db as db_module
from scanner.jobqueue import JobQueue


@pytest.fixture
async def queue(tmp_db: Path, migrations_dir: Path):
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    # JobQueue doesn't require a sources row, but FK is enforced — seed one.
    await conn.execute("INSERT INTO sources(name, rate_per_min) VALUES ('s1', 30), ('s2', 30)")
    await conn.commit()
    yield JobQueue(conn)
    await conn.close()


@pytest.mark.asyncio
async def test_enqueue_and_claim_one(queue: JobQueue) -> None:
    jid = await queue.enqueue("s1", cursor="c-init")
    assert jid > 0
    job = await queue.claim_one(claimed_by="worker-1")
    assert job is not None
    assert job.id == jid
    assert job.source == "s1"
    assert job.cursor == "c-init"
    assert job.status == "running"
    assert job.claimed_by == "worker-1"
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_claim_when_empty_returns_none(queue: JobQueue) -> None:
    assert await queue.claim_one(claimed_by="worker-1") is None


@pytest.mark.asyncio
async def test_claim_skips_running_jobs(queue: JobQueue) -> None:
    a = await queue.enqueue("s1")
    b = await queue.enqueue("s2")
    first = await queue.claim_one(claimed_by="w1")
    second = await queue.claim_one(claimed_by="w2")
    assert {first.id, second.id} == {a, b}
    # No more pending — third claim returns None
    assert await queue.claim_one(claimed_by="w3") is None


@pytest.mark.asyncio
async def test_mark_done(queue: JobQueue) -> None:
    jid = await queue.enqueue("s1")
    await queue.claim_one(claimed_by="w1")
    await queue.mark_done(jid)
    assert await queue.count_running() == 0
    assert await queue.count_pending() == 0


@pytest.mark.asyncio
async def test_mark_failed(queue: JobQueue) -> None:
    jid = await queue.enqueue("s1")
    await queue.claim_one(claimed_by="w1")
    await queue.mark_failed(jid, "boom")
    assert await queue.count_running() == 0


@pytest.mark.asyncio
async def test_defer_returns_job_to_pending_with_delay(queue: JobQueue) -> None:
    jid = await queue.enqueue("s1")
    await queue.claim_one(claimed_by="w1")
    await queue.defer(jid, retry_after_seconds=60)
    # count_pending excludes jobs with run_after in the future.
    assert await queue.count_pending() == 0
    assert await queue.count_running() == 0


@pytest.mark.asyncio
async def test_return_to_pending_is_immediately_claimable(queue: JobQueue) -> None:
    jid = await queue.enqueue("s1")
    await queue.claim_one(claimed_by="w1")
    await queue.return_to_pending(jid)
    again = await queue.claim_one(claimed_by="w2")
    assert again is not None and again.id == jid
    assert again.attempts == 2


@pytest.mark.asyncio
async def test_recover_stale_resurrects_long_running_jobs(queue: JobQueue) -> None:
    jid = await queue.enqueue("s1")
    await queue.claim_one(claimed_by="w1")
    # Backdate the claim by 1 hour.
    await queue._conn.execute(
        "UPDATE jobs SET claimed_at=datetime('now', '-1 hour') WHERE id=?", (jid,)
    )
    await queue._conn.commit()
    recovered = await queue.recover_stale(older_than_minutes=30)
    assert recovered == 1
    assert await queue.count_pending() == 1


@pytest.mark.asyncio
async def test_has_active_reflects_pending_and_running(queue: JobQueue) -> None:
    assert not await queue.has_active("s1")
    await queue.enqueue("s1")
    assert await queue.has_active("s1")
    await queue.claim_one(claimed_by="w")
    assert await queue.has_active("s1")
