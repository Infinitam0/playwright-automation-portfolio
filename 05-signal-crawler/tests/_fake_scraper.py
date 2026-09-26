"""FakeScraper for tests. Not auto-loaded by the convention loader (lives outside
scanner/scrapers/). Tests import it explicitly and register it inside a
clean_registry fixture so it never leaks across tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

from scanner.base import BaseScraper
from scanner.models import RawItem


class FakeScraper(BaseScraper):
    """Yields a fixed list of items. Lifecycle hooks record their calls so tests
    can assert setup/teardown were invoked."""

    default_rate_per_min = 6000  # effectively unbounded for tests

    def __init__(self) -> None:
        self.setup_called = 0
        self.teardown_called = 0
        self.items: list[RawItem] = [
            RawItem(
                source="fake",
                source_item_id=f"id-{i}",
                url=f"https://example.test/{i}",
                raw_content=f"body-{i}",
                cursor=f"c-{i}",
            )
            for i in range(3)
        ]

    async def setup(self, ctx=None) -> None:
        self.setup_called += 1

    async def teardown(self) -> None:
        self.teardown_called += 1

    async def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        for item in self.items:
            yield item
