"""Per-source token-bucket rate limiter.

The orchestrator calls `await rl.acquire(source)` immediately before each
inbox write — so scrapers that yield faster than the configured rate-per-min
naturally throttle without needing to know about the limit themselves.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Tokens accrue at `rate_per_min/60` per second; capacity is 2 seconds of
    accumulation, so brief bursts are allowed but a runaway scraper backs off."""

    def __init__(self, rate_per_min: int) -> None:
        if rate_per_min <= 0:
            raise ValueError(f"rate_per_min must be positive, got {rate_per_min}")
        self._rate = rate_per_min / 60.0
        self._capacity = max(1.0, self._rate * 2.0)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate
            await asyncio.sleep(wait)


class RateLimiter:
    """Owns one TokenBucket per source. `acquire(source)` is a no-op when the
    source has no configured limit (e.g. tests with FakeScraper bypass it)."""

    def __init__(self, source_rates: dict[str, int]) -> None:
        self._buckets: dict[str, TokenBucket] = {
            name: TokenBucket(rate) for name, rate in source_rates.items() if rate > 0
        }

    async def acquire(self, source: str) -> None:
        bucket = self._buckets.get(source)
        if bucket is None:
            return
        await bucket.acquire()
