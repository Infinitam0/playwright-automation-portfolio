"""Smoke-test: the FakeScraper helper itself satisfies the BaseScraper contract.
This catches FakeScraper drift before tests that depend on it (e2e in Commit 4)
start failing for cryptic reasons."""

from __future__ import annotations

import inspect

import pytest

from scanner.base import BaseScraper
from tests._fake_scraper import FakeScraper


def test_fake_scraper_is_a_basescraper() -> None:
    assert issubclass(FakeScraper, BaseScraper)


def test_fake_scraper_run_is_async_gen() -> None:
    assert inspect.isasyncgenfunction(FakeScraper.run)


@pytest.mark.asyncio
async def test_fake_scraper_yields_three_items() -> None:
    scraper = FakeScraper()
    await scraper.setup()
    items = [item async for item in scraper.run(since_cursor=None)]
    await scraper.teardown()
    assert len(items) == 3
    assert [it.source_item_id for it in items] == ["id-0", "id-1", "id-2"]
    assert [it.cursor for it in items] == ["c-0", "c-1", "c-2"]
    assert scraper.setup_called == 1
    assert scraper.teardown_called == 1
