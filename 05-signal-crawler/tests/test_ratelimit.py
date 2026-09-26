"""TokenBucket + RateLimiter tests."""

from __future__ import annotations

import time

import pytest

from scanner.ratelimit import RateLimiter, TokenBucket


@pytest.mark.asyncio
async def test_token_bucket_first_n_acquisitions_burst() -> None:
    """The 2-second burst capacity means the first few acquisitions don't block."""
    b = TokenBucket(rate_per_min=60)  # 1 token/sec, capacity ~2
    t0 = time.monotonic()
    for _ in range(2):
        await b.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"burst acquires should be near-instant, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_token_bucket_throttles_after_burst() -> None:
    """A 3rd acquisition must wait roughly 1 second at 60/min."""
    b = TokenBucket(rate_per_min=60)
    for _ in range(2):
        await b.acquire()
    t0 = time.monotonic()
    await b.acquire()
    elapsed = time.monotonic() - t0
    # Allow some slop. The next token at 1 token/sec means ~1s wait.
    assert 0.7 < elapsed < 1.3, f"throttled acquire should wait ~1s, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_token_bucket_rejects_zero_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)


@pytest.mark.asyncio
async def test_rate_limiter_no_op_for_unknown_source() -> None:
    """acquire(unknown) must not block — keeps tests with FakeScraper fast."""
    rl = RateLimiter({})
    t0 = time.monotonic()
    await rl.acquire("nonexistent")
    assert time.monotonic() - t0 < 0.05


@pytest.mark.asyncio
async def test_rate_limiter_uses_per_source_bucket() -> None:
    """High-rate source is fast; low-rate source is slow — they don't share state."""
    rl = RateLimiter({"fast": 6000, "slow": 60})
    # Burn the slow source down to 0 tokens (cap=2) — needs 3 acquires
    for _ in range(2):
        await rl.acquire("slow")
    # 'fast' should still be fast
    t0 = time.monotonic()
    for _ in range(2):
        await rl.acquire("fast")
    assert time.monotonic() - t0 < 0.05
