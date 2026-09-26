"""SQLite-backed job queue.

The orchestrator hands each worker a Job by calling `claim_one`, which atomically
picks the oldest eligible pending row, flips it to `running`, and stamps the
claimant. Workers terminate jobs with `mark_done` / `mark_failed` / `defer`.

`recover_stale` runs at startup. The scanner is single-process, so the
orchestrator recovers every `running` row (older_than_minutes=0): its claimant
is a crashed earlier process. Combined with
the upstream inbox UNIQUE constraint, this gives at-least-once delivery without
duplicate side-effects.
"""

from __future__ import annotations

import aiosqlite

from scanner.models import Job

_SELECT_COLS = "id, source, cursor, status, claimed_by, claimed_at, attempts, error_msg, run_after"


def _row_to_job(row: aiosqlite.Row | tuple | None) -> Job | None:
    if row is None:
        return None
    return Job(
        id=row[0],
        source=row[1],
        cursor=row[2],
        status=row[3],
        claimed_by=row[4],
        claimed_at=row[5],
        attempts=row[6],
        error_msg=row[7],
        run_after=row[8],
    )


class JobQueue:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def enqueue(self, source: str, cursor: str | None = None) -> int:
        cur = await self._conn.execute(
            "INSERT INTO jobs(source, cursor, status) VALUES (?, ?, 'pending') RETURNING id",
            (source, cursor),
        )
        row = await cur.fetchone()
        await cur.close()
        await self._conn.commit()
        return row[0]

    async def claim_one(self, claimed_by: str) -> Job | None:
        """Atomically claim the oldest eligible pending job. Returns None when empty."""
        cur = await self._conn.execute(
            f"""
            UPDATE jobs
            SET status='running',
                claimed_by=?,
                claimed_at=CURRENT_TIMESTAMP,
                attempts=attempts+1
            WHERE id = (
                SELECT id FROM jobs
                WHERE status='pending'
                  AND (run_after IS NULL OR run_after <= CURRENT_TIMESTAMP)
                ORDER BY id LIMIT 1
            )
            RETURNING {_SELECT_COLS}
            """,
            (claimed_by,),
        )
        row = await cur.fetchone()
        await cur.close()
        await self._conn.commit()
        return _row_to_job(row)

    async def mark_done(self, job_id: int) -> None:
        await self._conn.execute(
            "UPDATE jobs SET status='done', error_msg=NULL WHERE id=?",
            (job_id,),
        )
        await self._conn.commit()

    async def mark_failed(self, job_id: int, error_msg: str) -> None:
        await self._conn.execute(
            "UPDATE jobs SET status='failed', error_msg=? WHERE id=?",
            (error_msg, job_id),
        )
        await self._conn.commit()

    async def defer(self, job_id: int, retry_after_seconds: float) -> None:
        """Return a job to `pending`, schedule its retry via `run_after`."""
        await self._conn.execute(
            f"""
            UPDATE jobs
            SET status='pending',
                claimed_by=NULL,
                claimed_at=NULL,
                run_after=datetime('now', '+{int(retry_after_seconds)} seconds')
            WHERE id=?
            """,
            (job_id,),
        )
        await self._conn.commit()

    async def return_to_pending(self, job_id: int) -> None:
        """Return a job to pending without delay (for graceful-shutdown bailout)."""
        await self._conn.execute(
            """
            UPDATE jobs
            SET status='pending',
                claimed_by=NULL,
                claimed_at=NULL
            WHERE id=?
            """,
            (job_id,),
        )
        await self._conn.commit()

    async def recover_stale(self, older_than_minutes: int = 30) -> int:
        """Return any `running` job older than the cutoff back to `pending`.
        Returns the number of jobs recovered."""
        cur = await self._conn.execute(
            f"""
            UPDATE jobs
            SET status='pending',
                claimed_by=NULL,
                claimed_at=NULL
            WHERE status='running'
              AND claimed_at <= datetime('now', '-{int(older_than_minutes)} minutes')
            """,
        )
        recovered = cur.rowcount
        await cur.close()
        await self._conn.commit()
        return recovered

    async def count_pending(self) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='pending' "
            "AND (run_after IS NULL OR run_after <= CURRENT_TIMESTAMP)"
        )
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else 0

    async def count_running(self) -> int:
        cur = await self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running'")
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else 0

    async def has_active(self, source: str) -> bool:
        """True if there's any pending or running job for this source."""
        cur = await self._conn.execute(
            "SELECT 1 FROM jobs WHERE source=? AND status IN ('pending','running') LIMIT 1",
            (source,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None
