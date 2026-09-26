"""The orchestrator — top-level coordinator for the scanner.

Lifecycle:
  1. setup()              — open the DB connection, build queue + rate limiter,
                            seed `sources` rows from config, recover stale jobs.
  2. _enqueue_due()       — for each enabled source without an active job,
                            enqueue one (cursor populated from `cursors` table).
  3. spawn workers        — N async tasks pulling from the queue.
  4. spawn monitor        — drain mode (--once) shuts down when queue empties;
                            tick mode re-enqueues every 30s.
  5. signal handlers      — SIGINT/SIGTERM set the shutdown event; workers
                            finish current item, return job to pending, exit.
  6. teardown()           — close DB connection.

Worker loop pseudocode lives at the top of `_worker` so the file is readable.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import aiosqlite

from scanner import db as db_module
from scanner.base import BaseScraper, RateLimited, SelectorBroken
from scanner.config import Config
from scanner.jobqueue import JobQueue
from scanner.logging import get_logger
from scanner.models import Job, RawItem
from scanner.ratelimit import RateLimiter

log = get_logger(__name__)


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        scrapers: dict[str, type[BaseScraper]],
        *,
        drain_poll_seconds: float = 0.1,
        tick_seconds: float = 30.0,
        worker_idle_sleep: float = 1.0,
        pause_poll_seconds: float = 2.0,
    ) -> None:
        self._cfg = cfg
        self._scrapers = scrapers
        self._shutdown = asyncio.Event()
        self._conn: aiosqlite.Connection | None = None
        self._queue: JobQueue | None = None
        self._rl: RateLimiter | None = None
        self._drain_poll_seconds = drain_poll_seconds
        self._tick_seconds = tick_seconds
        self._worker_idle_sleep = worker_idle_sleep
        self._pause_poll_seconds = pause_poll_seconds

    # ---------- public entrypoint ----------

    async def run(self, once: bool = False) -> None:
        await self._setup()
        try:
            self._install_signal_handlers()
            await self._enqueue_due()

            workers = [
                asyncio.create_task(self._worker(i), name=f"worker-{i}")
                for i in range(max(1, self._cfg.workers))
            ]
            monitor = asyncio.create_task(
                self._drain_monitor() if once else self._tick_monitor(),
                name="monitor",
            )
            tasks = workers + [monitor]
            try:
                await self._shutdown.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._teardown()

    # ---------- setup / teardown ----------

    async def _setup(self) -> None:
        self._shutdown.clear()
        self._conn = await db_module.connect(self._cfg.db_path)
        self._queue = JobQueue(self._conn)
        self._rl = RateLimiter({n: s.rate_per_min for n, s in self._cfg.sources.items()})
        await self._seed_sources()
        # Single-process by design, so any `running` job at startup belongs to a
        # dead process, however recently it was claimed.
        recovered = await self._queue.recover_stale(older_than_minutes=0)
        if recovered:
            log.info("orchestrator.recover", stale_jobs=recovered)
        log.info(
            "orchestrator.setup",
            db_path=str(self._cfg.db_path),
            workers=self._cfg.workers,
            sources=sorted(self._scrapers.keys()),
        )

    async def _teardown(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _seed_sources(self) -> None:
        for name, sc in self._cfg.sources.items():
            await self._conn.execute(
                """
                INSERT INTO sources(name, enabled, rate_per_min)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    enabled=excluded.enabled,
                    rate_per_min=excluded.rate_per_min
                """,
                (name, int(sc.enabled), sc.rate_per_min),
            )
        await self._conn.commit()

    # ---------- job enqueue / cursor ----------

    async def _enqueue_due(self) -> None:
        for name, sc in self._cfg.sources.items():
            if not sc.enabled:
                continue
            if name not in self._scrapers:
                log.debug("orchestrator.skip_unknown_source", source=name)
                continue
            if await self._queue.has_active(name):
                continue
            cursor = await self._read_cursor(name)
            job_id = await self._queue.enqueue(source=name, cursor=cursor)
            log.info("job.enqueued", source=name, job_id=job_id, cursor=cursor)

    async def _read_cursor(self, source: str) -> str | None:
        cur = await self._conn.execute("SELECT max_id FROM cursors WHERE source=?", (source,))
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def _commit_cursor(self, source: str, cursor: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO cursors(source, max_id) VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE
            SET max_id=excluded.max_id,
                processed_at=CURRENT_TIMESTAMP
            """,
            (source, cursor),
        )
        await self._conn.commit()

    async def _upsert_inbox(self, item: RawItem) -> None:
        await self._conn.execute(
            """
            INSERT INTO inbox(source, source_item_id, url, raw_content, hash)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, source_item_id) DO UPDATE
            SET raw_content=excluded.raw_content,
                hash=excluded.hash,
                extracted=CASE WHEN inbox.hash != excluded.hash THEN 0 ELSE inbox.extracted END,
                scraped_at=CURRENT_TIMESTAMP
            """,
            (item.source, item.source_item_id, item.url, item.raw_content, item.hash),
        )
        await self._conn.commit()

    # ---------- control / pause ----------

    async def _is_paused_global(self) -> bool:
        cur = await self._conn.execute("SELECT value FROM control WHERE key='paused'")
        row = await cur.fetchone()
        await cur.close()
        return row is not None and row[0] == "1"

    async def _is_paused_source(self, source: str) -> bool:
        cur = await self._conn.execute(
            "SELECT value FROM control WHERE key=?",
            (f"paused:{source}",),
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None and row[0] == "1"

    async def _set_control(self, key: str, value: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO control(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    # ---------- workers ----------

    async def _worker(self, wid: int) -> None:
        """
        loop:
            shutdown? -> exit
            paused?   -> sleep(pause_poll), retry
            claim_one -> got job? run it. else sleep(idle).
        """
        worker_name = f"w{wid}"
        log.info("worker.start", worker=worker_name)
        try:
            while not self._shutdown.is_set():
                if await self._is_paused_global():
                    await asyncio.sleep(self._pause_poll_seconds)
                    continue

                job = await self._queue.claim_one(claimed_by=worker_name)
                if job is None:
                    await asyncio.sleep(self._worker_idle_sleep)
                    continue

                if await self._is_paused_source(job.source):
                    await self._queue.defer(job.id, self._pause_poll_seconds)
                    continue

                if job.source not in self._scrapers:
                    await self._queue.mark_failed(
                        job.id, f"no scraper registered for source: {job.source}"
                    )
                    continue

                await self._run_job(job, worker=worker_name)
        except asyncio.CancelledError:
            pass
        finally:
            log.info("worker.stop", worker=worker_name)

    async def _run_job(self, job: Job, *, worker: str) -> None:
        scraper_cls = self._scrapers[job.source]
        scraper = scraper_cls()
        log.info(
            "job.start",
            worker=worker,
            job_id=job.id,
            source=job.source,
            cursor=job.cursor,
            attempt=job.attempts,
        )
        items_count = 0
        # Committed when the job completes: HN/Reddit emit a newest-first
        # high-water mark, so committing it mid-job would make the next job skip
        # every older item a failed job never reached. Scrapers with an
        # in-order position (checkpoint_cursor) also commit per item.
        last_cursor: str | None = None
        try:
            await scraper.setup()
            try:
                async for item in scraper.run(job.cursor):
                    if self._shutdown.is_set():
                        log.info(
                            "job.shutdown_during",
                            job_id=job.id,
                            source=job.source,
                            items=items_count,
                        )
                        await self._queue.return_to_pending(job.id)
                        return
                    await self._rl.acquire(job.source)
                    await self._upsert_inbox(item)
                    if item.cursor:
                        last_cursor = item.cursor
                        if scraper.checkpoint_cursor:
                            await self._commit_cursor(job.source, item.cursor)
                    items_count += 1
            finally:
                await scraper.teardown()
            if last_cursor:
                await self._commit_cursor(job.source, last_cursor)
            await self._queue.mark_done(job.id)
            log.info("job.done", job_id=job.id, source=job.source, items=items_count)
        except RateLimited as e:
            log.info(
                "job.rate_limited",
                job_id=job.id,
                source=job.source,
                retry_after=e.retry_after,
            )
            await self._queue.defer(job.id, e.retry_after)
            await self._set_control(f"paused:{job.source}", "1")
        except SelectorBroken as e:
            log.error("job.selector_broken", job_id=job.id, source=job.source, detail=str(e))
            await self._queue.mark_failed(job.id, f"SelectorBroken: {e}")
            await self._set_control(f"needs_review:{job.source}", "1")
        except asyncio.CancelledError:
            await self._queue.return_to_pending(job.id)
            raise
        except Exception as e:  # noqa: BLE001 — catch-all to keep workers alive
            log.exception("job.error", job_id=job.id, source=job.source)
            await self._queue.mark_failed(job.id, f"{type(e).__name__}: {e}")

    # ---------- monitors ----------

    async def _drain_monitor(self) -> None:
        """Once-mode: shutdown when there's nothing pending and nothing running."""
        try:
            while not self._shutdown.is_set():
                await asyncio.sleep(self._drain_poll_seconds)
                if self._shutdown.is_set():
                    return
                pending = await self._queue.count_pending()
                running = await self._queue.count_running()
                if pending == 0 and running == 0:
                    log.info("orchestrator.drain_complete")
                    self._shutdown.set()
                    return
        except asyncio.CancelledError:
            pass

    async def _tick_monitor(self) -> None:
        """Long-running mode: periodically re-enqueue work."""
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=self._tick_seconds)
                    return  # shutdown set during wait
                except TimeoutError:
                    pass
                await self._enqueue_due()
        except asyncio.CancelledError:
            pass

    # ---------- signal handlers ----------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def handler(signum, _frame=None):
            log.info("orchestrator.signal", signum=int(signum) if signum else -1)
            loop.call_soon_threadsafe(self._shutdown.set)

        signal.signal(signal.SIGINT, handler)
        if sys.platform != "win32":
            with contextlib.suppress(AttributeError, ValueError):
                signal.signal(signal.SIGTERM, handler)
