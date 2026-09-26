"""Per-host politeness: serialise requests to the same host and enforce a
minimum jittered interval between them, while capping global concurrency.

A single global `jittered_delay` is not enough for a multi-domain crawler: it
needs per-host throttling so one slow host never paces every other host.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class PerHostRateLimiter:
    def __init__(
        self,
        min_interval: float,
        jitter_factor: float = 0.5,
        max_concurrency: int = 4,
    ) -> None:
        self.min_interval = min_interval
        self.jitter_factor = jitter_factor
        self._sem = asyncio.Semaphore(max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, host: str, min_interval: float | None = None):
        """Space requests to one host, THEN take a global concurrency slot.

        Order matters. Acquiring the semaphore first meant a task waiting out a
        host's interval sat on one of the (default four) slots doing nothing, so
        four pages of one slow host held every slot asleep and starved every
        other host: throughput collapsed toward one request per `min_interval`
        globally, however many hosts were queued. Waiting on the
        per-host lock costs nothing now, because it holds no shared resource.

        Lock order is the same for every caller -- host lock, then semaphore --
        so there is no inversion to deadlock on.
        """
        # A host's own robots.txt Crawl-delay can only slow us down, never speed
        # us up: our configured floor still applies if the host asks for less.
        interval = self.min_interval
        if min_interval is not None:
            interval = max(interval, min_interval)
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        await lock.acquire()
        try:
            last = self._last_request.get(host)
            if last is not None:
                elapsed = time.monotonic() - last
                jitter = interval * self.jitter_factor
                wait = interval - elapsed + random.uniform(0, jitter)
                if wait > 0:
                    logger.debug(f"Rate-limit {host}: sleeping {wait:.2f}s")
                    await asyncio.sleep(wait)
            await self._sem.acquire()
            try:
                yield
            finally:
                # Measured from the end of the request, as before, so the
                # interval is a gap between requests and not between starts.
                self._last_request[host] = time.monotonic()
                self._sem.release()
        finally:
            lock.release()
