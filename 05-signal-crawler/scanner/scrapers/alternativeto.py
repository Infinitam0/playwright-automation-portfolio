"""AlternativeTo scraper.

Job: confirm and discover the AlternativeTo "Discontinued" badge for apps the
scanner cares about. We treat AT as a ground-truth oracle for `discontinued`
status — when AT marks an app discontinued, the catalog's `discontinued: true`
gets boosted via the scoring formula's `badge` term.

Strategy (v1): catalog-driven confirmation.
  For each canonical app name in known_apps.yaml, slugify and visit
  https://alternativeto.net/software/{slug}/about/. Parse the page text for
  the "Discontinued" marker. Yield a RawItem when present. Cursor is the
  index into the candidate list, so resume picks up where we left off.

Discovery of *new* discontinued apps (e.g. browsing category listings) is a
v2 enhancement. Today we lean on HN for discovery and AT for confirmation.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scanner.base import BaseScraper, RateLimited, SelectorBroken
from scanner.browser import BrowserSession
from scanner.extract.catalog import Catalog
from scanner.logging import get_logger
from scanner.models import RawItem
from scanner.registry import register_scraper

log = get_logger(__name__)


@register_scraper("alternativeto")
class AlternativeToScraper(BaseScraper):
    default_rate_per_min: ClassVar[int] = 20
    checkpoint_cursor: ClassVar[bool] = True  # catalog position, yielded in order

    BASE_URL: ClassVar[str] = "https://alternativeto.net"
    NAV_TIMEOUT_MS: ClassVar[int] = 15_000

    # Regex that flags an AT page as discontinued. AT shows a card/section
    # labeled "Alerts" that contains the word "Discontinued"; or a phrase
    # like "X shut down on Date". Both are reliable.
    DISCONTINUED_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:Alerts[\s\S]{0,200}Discontinued|shut\s+down\s+on)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        candidates: list[str] | None = None,
        catalog_path: str | Path = "known_apps.yaml",
        session: BrowserSession | None = None,
    ) -> None:
        self._candidates_override = candidates
        self._catalog_path = Path(catalog_path)
        self._session = session
        self._owns_session = session is None

    async def setup(self, ctx: Any = None) -> None:
        if self._session is None:
            self._session = BrowserSession()
            await self._session.launch()

    async def teardown(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def run(self, since_cursor: str | None) -> AsyncIterator[RawItem]:
        assert self._session is not None, "setup() must be called first"
        candidates = self._candidates_override or self._candidates_from_catalog()
        start_index = self._parse_cursor(since_cursor)
        if start_index >= len(candidates):
            return

        page = await self._session.new_page()
        try:
            confirmed_any = False
            for i in range(start_index, len(candidates)):
                name = candidates[i]
                slug = self._slugify(name)
                url = f"{self.BASE_URL}/software/{slug}/about/"
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.NAV_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError:
                    log.warning("at.timeout", name=name, url=url)
                    continue
                except PlaywrightError as e:
                    log.warning("at.nav_error", name=name, url=url, err=str(e))
                    continue

                if response is None:
                    continue
                status = response.status
                if status == 404:
                    continue
                if status == 429:
                    retry_after = float(response.headers.get("retry-after") or 60)
                    raise RateLimited(retry_after, detail="AT 429")
                if status >= 500:
                    log.warning("at.server_error", status=status, name=name)
                    continue
                if status != 200:
                    log.warning("at.unexpected_status", status=status, name=name)
                    continue

                html = await page.content()
                if self.is_discontinued(html):
                    confirmed_any = True
                    yield RawItem(
                        source="alternativeto",
                        source_item_id=slug,
                        url=url,
                        raw_content=f"{name} is discontinued (per AlternativeTo)",
                        cursor=str(i + 1),
                    )
                # else: keep moving — we only yield positive confirmations.

            # If we visited the whole list and found nothing discontinued, the
            # selector or page layout has likely shifted under us. (AT always
            # has some discontinued apps in any seeded catalog.)
            if not confirmed_any and len(candidates) - start_index >= 5:
                raise SelectorBroken(
                    "AT: scanned >=5 candidates and found 0 discontinued — "
                    "DISCONTINUED_RE may be stale"
                )
        finally:
            await page.close()

    # ---- pure helpers (unit-tested) ----

    @classmethod
    def is_discontinued(cls, html: str) -> bool:
        return bool(cls.DISCONTINUED_RE.search(html))

    @staticmethod
    def _slugify(name: str) -> str:
        """AT's URL convention: lowercase, alphanumeric + hyphens."""
        s = name.lower()
        s = re.sub(r"[^a-z0-9\s-]", "", s)
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"-+", "-", s)
        return s.strip("-")

    @staticmethod
    def _parse_cursor(since_cursor: str | None) -> int:
        if not since_cursor:
            return 0
        try:
            v = int(since_cursor)
            return max(0, v)
        except (TypeError, ValueError):
            return 0

    def _candidates_from_catalog(self) -> list[str]:
        cat = Catalog.load(self._catalog_path)
        # Iterating .resolve() reach over private _entries kept stable.
        names = sorted(cat._entries.keys())
        return names
