"""BaseScraper ABC — the "atom" contract every scraper must implement.

A scraper:
- Declares `name` (registry key, usually via @register_scraper)
- Declares `default_rate_per_min` (used if config doesn't override)
- Optionally implements `setup(ctx)` to acquire its client/browser/etc.
- Implements `run(since_cursor)` as an async generator yielding RawItem
- Optionally implements `teardown()` to release resources

Lifecycle expressed through exceptions:
- raise RateLimited(retry_after) on 429 / captcha-sentinel  → orchestrator pauses source
- raise SelectorBroken(msg) when parse yields nothing       → orchestrator marks needs_review
- generator exhausts naturally                              → job is marked done
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from scanner.models import RawItem


class RateLimited(Exception):
    """Raise to ask the orchestrator to pause this source for `retry_after` seconds."""

    def __init__(self, retry_after: float, detail: str | None = None) -> None:
        super().__init__(detail or f"rate limited; retry after {retry_after}s")
        self.retry_after = float(retry_after)


class SelectorBroken(Exception):
    """Raise when parsing returns zero items where >0 was expected — indicates
    the source's HTML structure has changed and the scraper needs human attention.
    """


class BaseScraper(ABC):
    """Inherit, set `default_rate_per_min`, implement `run`, decorate with
    @register_scraper("name"). That's the whole contract."""

    name: ClassVar[str] = ""
    default_rate_per_min: ClassVar[int] = 30

    async def setup(self, ctx: Any = None) -> None:
        """Acquire whatever client/state the scraper needs. Default is no-op.

        `ctx` is reserved for future shared resources (e.g. a pooled browser
        context). In v1 the orchestrator passes None — scrapers that need a
        client build one themselves here.
        """

    async def teardown(self) -> None:
        """Release resources acquired in setup. Default is no-op."""

    @classmethod
    def text(cls, raw_content: str) -> str:
        """Extract the searchable text from a stored raw_content blob.

        The extractor calls this when a scraper's raw_content is not plain text
        (e.g. the HN scraper stores JSON). Default: the raw_content is itself
        the text. Override per-source as needed.
        """
        return raw_content

    @abstractmethod
    def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        """Yield RawItem instances. Async generator.

        - `since_cursor` is the last cursor previously committed for this source,
          or None on the first run.
        - When `RawItem.cursor` is set on a yielded item, the orchestrator commits
          it to the cursors table after the inbox upsert succeeds.
        - To request a pause, raise RateLimited(retry_after).
        - When the source structure has shifted, raise SelectorBroken("...").
        """
        raise NotImplementedError
