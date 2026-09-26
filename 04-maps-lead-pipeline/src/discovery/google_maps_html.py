"""Google Maps HTML scrape — OPT-IN, high-volume, higher-risk fallback.

Only runs when explicitly selected (`--source maps`) AND opted in
(`LEADS_MAPS_ENABLED=true`). The Places API is the default and safest source;
this exists for coverage the API misses. Queries run one at a time with
jittered pauses. If a challenge page is detected (`assert_no_challenge`), the
query is abandoned and returns no results; it is never retried.

Extraction is intentionally shallow: name + the maps place URL from the results
feed. Listings are NOT clicked through (that would multiply load on Google), so
a maps-only row has no website or phone until another source supplies one.
If Patchright is unavailable or the feed markup changes, it returns [] (logged).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..models import RawCandidate, Source
from ..scraper.browser import assert_no_challenge, create_browser_session
from ..scraper.timing import jittered_delay
from .base import DiscoverySource, SearchTask

logger = logging.getLogger(__name__)

_PLACE_LINK_RE = re.compile(r"/maps/place/")


class GoogleMapsHtmlSource(DiscoverySource):
    name = "maps"

    def __init__(
        self, profile_dir: Path, *, max_scrolls: int = 5, enabled: bool = False
    ) -> None:
        self._profile_dir = profile_dir
        self._max_scrolls = max_scrolls
        self._enabled = enabled

    async def search(self, task: SearchTask) -> list[RawCandidate]:
        if not self._enabled:
            logger.warning(
                "maps: Google disallows automated scraping of Maps (robots.txt); "
                "skipping. Set LEADS_MAPS_ENABLED=true to opt in."
            )
            return []
        try:
            return await self._run(task)
        except RuntimeError as e:  # patchright missing
            logger.warning(f"maps: {e}")
            return []
        except Exception as e:  # noqa: BLE001
            logger.info(f"maps: '{task.query}' failed: {e}")
            return []

    async def _run(self, task: SearchTask) -> list[RawCandidate]:
        url = f"https://www.google.com/maps/search/{task.query.replace(' ', '+')}"
        out: list[RawCandidate] = []
        async with create_browser_session(self._profile_dir) as context:
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await jittered_delay(3.0, 0.5)
            await assert_no_challenge(page)

            feed = 'div[role="feed"]'
            for _ in range(self._max_scrolls):
                try:
                    await page.eval_on_selector(
                        feed, "el => el.scrollTop = el.scrollHeight"
                    )
                except Exception:  # noqa: BLE001
                    break
                await jittered_delay(2.0, 0.5)

            links = await page.query_selector_all(f'{feed} a[href*="/maps/place/"]')
            seen: set[str] = set()
            for link in links:
                name = (await link.get_attribute("aria-label")) or ""
                href = (await link.get_attribute("href")) or ""
                name = name.strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append(
                    RawCandidate(
                        name=name,
                        city=task.city,
                        province=task.province,
                        source=Source(name=self.name, source_id=name, url=href),
                    )
                )
        logger.info(f"maps: '{task.query}' -> {len(out)} candidates")
        return out
