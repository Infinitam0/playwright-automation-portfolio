"""HackerNews scraper via the Algolia HN Search API.

Algolia exposes HN as a fully-indexed search index — far better suited to our
needs (find posts mentioning replacement-seeking phrases) than the official
Firebase API, which is item-by-id only.

We iterate a list of replacement-seeking query phrases. For each, we paginate
by date descending, filtered to items newer than the saved cursor. The cursor
is the highest `created_at_i` (epoch seconds) seen across all queries on this
run. On first run (no cursor), we look back DEFAULT_LOOKBACK_DAYS.

No auth, no browser. Public endpoint:
    GET https://hn.algolia.com/api/v1/search_by_date

Algolia caps pagination at 50 pages × 100 hitsPerPage = 5000 results per query.
That's plenty for our use case — we're not trying to index all of HN.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from scanner.base import BaseScraper, RateLimited
from scanner.models import RawItem
from scanner.registry import register_scraper


@register_scraper("hackernews")
class HackernewsScraper(BaseScraper):
    default_rate_per_min: ClassVar[int] = 60  # Algolia public rate limit

    SEARCH_URL: ClassVar[str] = "https://hn.algolia.com/api/v1/search_by_date"
    HITS_PER_PAGE: ClassVar[int] = 100
    MAX_PAGES_PER_QUERY: ClassVar[int] = 50
    DEFAULT_LOOKBACK_DAYS: ClassVar[int] = 7
    USER_AGENT: ClassVar[str] = "signal-crawler/0.1 (+https://example.invalid)"

    # Phrases optimised for high precision. "alternative to" is the highest-recall
    # phrase; the shutdown/EOL ones are lower-recall but higher-precision signals.
    QUERIES: ClassVar[list[str]] = [
        '"alternative to"',
        '"replacement for"',
        '"is shutting down"',
        '"is discontinued"',
        '"is dead"',
        '"moving away from"',
    ]

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client: httpx.AsyncClient | None = client
        self._owns_client: bool = client is None

    async def setup(self, ctx: Any = None) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": self.USER_AGENT, "Accept": "application/json"},
            )

    async def teardown(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        since_ts = self._parse_since(since_cursor)
        max_seen = since_ts
        assert self._client is not None, "setup() must be called before run()"

        for query in self.QUERIES:
            page = 0
            while page < self.MAX_PAGES_PER_QUERY:
                params = {
                    "query": query,
                    "tags": "(story,comment)",
                    "numericFilters": f"created_at_i>{since_ts}",
                    "hitsPerPage": self.HITS_PER_PAGE,
                    "page": page,
                }
                resp = await self._client.get(self.SEARCH_URL, params=params)

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "60") or 60)
                    raise RateLimited(retry_after, detail="HN Algolia 429")

                resp.raise_for_status()
                data = resp.json()
                hits = data.get("hits", [])
                if not hits:
                    break

                for hit in hits:
                    item = self._parse_hit(hit)
                    if item is None:
                        continue
                    ts = int(hit.get("created_at_i") or 0)
                    if ts > max_seen:
                        max_seen = ts
                    # The running max-seen rides on every item; the orchestrator
                    # commits the last one only when the whole job succeeds.
                    yield RawItem(
                        source=item.source,
                        source_item_id=item.source_item_id,
                        url=item.url,
                        raw_content=item.raw_content,
                        cursor=str(max_seen),
                    )

                nb_pages = int(data.get("nbPages") or 0)
                page += 1
                if page >= nb_pages:
                    break

    @classmethod
    def text(cls, raw_content: str) -> str:
        """Concatenate the searchable fields from an Algolia HN hit JSON."""
        try:
            d = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content
        parts = [d.get("title"), d.get("story_text"), d.get("comment_text")]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _parse_hit(hit: dict[str, Any]) -> RawItem | None:
        obj_id = hit.get("objectID")
        if not obj_id:
            return None
        return RawItem(
            source="hackernews",
            source_item_id=str(obj_id),
            url=f"https://news.ycombinator.com/item?id={obj_id}",
            raw_content=json.dumps(hit, sort_keys=True),
        )

    @classmethod
    def _parse_since(cls, since_cursor: str | None) -> int:
        if since_cursor:
            try:
                return int(since_cursor)
            except (TypeError, ValueError):
                pass
        return int((datetime.now(UTC) - timedelta(days=cls.DEFAULT_LOOKBACK_DAYS)).timestamp())
