"""HackerNews scraper tests using httpx MockTransport (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scanner.base import RateLimited
from scanner.scrapers.hackernews import HackernewsScraper

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"


def _load_snapshot() -> dict:
    return json.loads((SNAPSHOTS / "hn_api.json").read_text(encoding="utf-8"))


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- pure parser ----


def test_parse_hit_skips_when_object_id_missing() -> None:
    snapshot = _load_snapshot()
    skipped = next(h for h in snapshot["hits"] if not h.get("objectID"))
    assert HackernewsScraper._parse_hit(skipped) is None


def test_parse_hit_populates_url_from_object_id() -> None:
    hit = next(h for h in _load_snapshot()["hits"] if h.get("objectID"))
    item = HackernewsScraper._parse_hit(hit)
    assert item is not None
    assert item.source == "hackernews"
    assert item.url == f"https://news.ycombinator.com/item?id={hit['objectID']}"
    # raw_content is deterministic JSON
    assert json.loads(item.raw_content) == hit


def test_parse_since_default_is_seven_days_ago() -> None:
    import time

    seven_days_ago = int(time.time()) - 7 * 86_400
    val = HackernewsScraper._parse_since(None)
    assert seven_days_ago - 5 <= val <= seven_days_ago + 5


def test_parse_since_passthrough() -> None:
    assert HackernewsScraper._parse_since("1700000000") == 1700000000


def test_parse_since_falls_back_on_garbage() -> None:
    """A malformed cursor must not crash run; we fall back to the lookback default."""
    val = HackernewsScraper._parse_since("not-an-int")
    import time

    assert val < int(time.time())


# ---- end-to-end with MockTransport ----


@pytest.mark.asyncio
async def test_run_yields_items_from_mocked_response() -> None:
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(str(request.url))
        # Return snapshot for the first page of every query; empty for subsequent.
        page = int(request.url.params.get("page", "0"))
        if page > 0:
            return httpx.Response(200, json={"hits": [], "nbPages": 1})
        return httpx.Response(200, json=_load_snapshot())

    client = _mock_client(handler)
    scraper = HackernewsScraper(client=client)
    await scraper.setup()
    try:
        items = [item async for item in scraper.run(since_cursor="0")]
    finally:
        await scraper.teardown()

    # 3 valid hits (the empty-objectID one is skipped) × 6 queries = 18
    assert len(items) == 6 * 3
    assert all(it.source == "hackernews" for it in items)
    assert all(it.source_item_id for it in items)
    assert all(it.cursor is not None for it in items)
    # Cursor monotonically non-decreasing
    cursors = [int(it.cursor) for it in items]
    assert cursors == sorted(cursors)
    # Each query produces a request
    assert len(requests_seen) >= 6


@pytest.mark.asyncio
async def test_run_raises_rate_limited_on_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "42"}, json={"error": "slow down"})

    scraper = HackernewsScraper(client=_mock_client(handler))
    await scraper.setup()
    try:
        with pytest.raises(RateLimited) as exc_info:
            async for _ in scraper.run(since_cursor="0"):
                break
        assert exc_info.value.retry_after == 42.0
    finally:
        await scraper.teardown()


@pytest.mark.asyncio
async def test_run_raises_on_5xx() -> None:
    """Generic HTTP errors propagate so the orchestrator marks the job failed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    scraper = HackernewsScraper(client=_mock_client(handler))
    await scraper.setup()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in scraper.run(since_cursor="0"):
                break
    finally:
        await scraper.teardown()


@pytest.mark.asyncio
async def test_run_stops_paginating_when_hits_empty() -> None:
    """An empty `hits` response breaks the loop for that query."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "0"))
        calls.append(page)
        return httpx.Response(200, json={"hits": [], "nbPages": 0})

    scraper = HackernewsScraper(client=_mock_client(handler))
    await scraper.setup()
    try:
        items = [item async for item in scraper.run(since_cursor="0")]
    finally:
        await scraper.teardown()

    assert items == []
    # One call per query (6) — none paginated beyond page 0
    assert calls == [0] * 6
