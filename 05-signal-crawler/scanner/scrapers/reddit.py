"""Reddit scraper for old.reddit.com search pages.

old.reddit.com serves search results as static HTML with stable markup and
standard "?after=t3_<id>" pagination, which keeps the parser small and
testable offline against saved snapshots. This is a demo of the technique:
real use should go through Reddit's official Data API (OAuth, documented
rate limits) and follow Reddit's terms.

We iterate the same replacement-seeking phrase set as the HN scraper, hit
/search for each, and walk the result pages. Cursor is the highest post
timestamp seen across all queries; subsequent runs filter via Reddit's
search "t=" time-window (we pick the smallest window that includes the
cursor).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlencode

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scanner.base import BaseScraper, RateLimited, SelectorBroken
from scanner.browser import BrowserSession
from scanner.logging import get_logger
from scanner.models import RawItem
from scanner.registry import register_scraper

log = get_logger(__name__)


@register_scraper("reddit")
class RedditScraper(BaseScraper):
    default_rate_per_min: ClassVar[int] = 30

    BASE_URL: ClassVar[str] = "https://old.reddit.com"
    SEARCH_PATH: ClassVar[str] = "/search"
    NAV_TIMEOUT_MS: ClassVar[int] = 20_000
    MAX_PAGES_PER_QUERY: ClassVar[int] = 5

    QUERIES: ClassVar[list[str]] = [
        '"alternative to"',
        '"replacement for"',
        '"is shutting down"',
        '"is discontinued"',
        '"moving away from"',
    ]

    # Reddit shows "Are you a human?" captcha pages with this fingerprint.
    CAPTCHA_RE: ClassVar[re.Pattern[str]] = re.compile(r"(?i)(captcha|recaptcha|over_18\?)")

    def __init__(self, session: BrowserSession | None = None) -> None:
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
        since_ts = self._parse_since(since_cursor)
        max_seen = since_ts
        time_window = self._time_window_for(since_ts)

        page = await self._session.new_page()
        try:
            yielded_any = False
            for query in self.QUERIES:
                after_token: str | None = None
                for page_idx in range(self.MAX_PAGES_PER_QUERY):
                    params = {
                        "q": query,
                        "sort": "new",
                        "restrict_sr": "",
                        "t": time_window,
                    }
                    if after_token:
                        params["after"] = after_token
                    url = f"{self.BASE_URL}{self.SEARCH_PATH}?{urlencode(params)}"

                    try:
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=self.NAV_TIMEOUT_MS,
                        )
                    except PlaywrightTimeoutError:
                        log.warning("reddit.timeout", url=url)
                        break
                    except PlaywrightError as e:
                        log.warning("reddit.nav_error", url=url, err=str(e))
                        break

                    if response is None:
                        break
                    status = response.status
                    if status == 429:
                        retry_after = float(response.headers.get("retry-after") or 60)
                        raise RateLimited(retry_after, detail="reddit 429")
                    if status == 403:
                        # Reddit's anti-bot returns 403 when it suspects a bot.
                        raise RateLimited(120.0, detail="reddit 403 (anti-bot)")
                    if status != 200:
                        log.warning("reddit.unexpected_status", status=status, url=url)
                        break

                    html = await page.content()
                    if self.CAPTCHA_RE.search(html):
                        raise RateLimited(300.0, detail="reddit captcha challenge")

                    results = self.parse_results(html)
                    if not results and page_idx == 0 and query == self.QUERIES[0]:
                        # First page of first query empty -> probable selector breakage.
                        raise SelectorBroken("reddit: 0 results on first search page")

                    for r in results:
                        ts = int(r.get("created_utc") or 0)
                        if ts and ts <= since_ts:
                            continue
                        if ts > max_seen:
                            max_seen = ts
                        yielded_any = True
                        yield RawItem(
                            source="reddit",
                            source_item_id=r["id"],
                            url=r.get("permalink") or "",
                            raw_content=json.dumps(r, sort_keys=True),
                            cursor=str(max_seen),
                        )

                    after_token = self.parse_next_after(html)
                    if not after_token:
                        break

            if not yielded_any:
                # All queries exhausted without a single result: very unusual.
                log.info("reddit.run_complete_no_results")
        finally:
            await page.close()

    # ---- pure helpers ----

    @classmethod
    def text(cls, raw_content: str) -> str:
        try:
            d = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content
        return " ".join(p for p in (d.get("title"), d.get("selftext"), d.get("subreddit")) if p)

    @staticmethod
    def parse_results(html: str) -> list[dict[str, Any]]:
        """Extract search results from old.reddit.com search HTML.

        Uses a couple of stable patterns; we don't pull in BeautifulSoup just
        for these. Each result yields a dict with: id (t3_xxx), title, subreddit,
        permalink, created_utc, selftext."""
        results: list[dict[str, Any]] = []

        # Each result is wrapped like:
        #   <div class=" thing ... id-t3_abc123 ..." data-fullname="t3_abc123" ...>
        # Fallback: just grab data-fullname blocks, even without the closing comment.
        for m in re.finditer(r'data-fullname="(t3_[a-z0-9]+)"', html, re.IGNORECASE):
            post_id = m.group(1)
            # Find a window of HTML around this id.
            start = max(0, m.start() - 100)
            end = min(len(html), m.start() + 4000)
            chunk = html[start:end]
            entry = _parse_thing_chunk(post_id, chunk)
            if entry:
                results.append(entry)
        return _dedupe_by_id(results)

    @staticmethod
    def parse_next_after(html: str) -> str | None:
        """Extract the `after=` token for pagination from the 'next' link."""
        m = re.search(
            r'class="[^"]*\bnext-button\b[^"]*"[^>]*>\s*<a[^>]*href="[^"]*?(?:&(?:amp;)?|\?)after=(t3_[a-z0-9]+)',
            html,
            re.IGNORECASE,
        )
        return m.group(1) if m else None

    @staticmethod
    def _parse_since(since_cursor: str | None) -> int:
        if not since_cursor:
            return 0
        try:
            return max(0, int(since_cursor))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _time_window_for(since_ts: int) -> str:
        """Pick the smallest Reddit time-window that includes since_ts.

        Reddit accepts: hour, day, week, month, year, all.
        """
        if since_ts <= 0:
            return "month"
        age = datetime.now(UTC).timestamp() - since_ts
        if age <= 3_600:
            return "hour"
        if age <= 86_400:
            return "day"
        if age <= 7 * 86_400:
            return "week"
        if age <= 31 * 86_400:
            return "month"
        if age <= 365 * 86_400:
            return "year"
        return "all"


# ---- helpers used by parse_results ----

_TITLE_RE = re.compile(
    r'<a[^>]*\bclass="[^"]*\b(?:search-title|title)\b[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SUBREDDIT_RE = re.compile(
    r'<a[^>]*href="/r/([A-Za-z0-9_]+)/?"[^>]*>(?:r/)?[A-Za-z0-9_]+</a>',
    re.IGNORECASE,
)
_PERMALINK_RE = re.compile(
    r'data-permalink="(/r/[^"]+/comments/[^"]+/)"',
    re.IGNORECASE,
)
_TIME_RE = re.compile(r'<time[^>]*\bdatetime="([^"]+)"', re.IGNORECASE)
_SELFTEXT_RE = re.compile(
    r'<div[^>]*\bclass="[^"]*\b(?:md|search-expando)\b[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_thing_chunk(post_id: str, chunk: str) -> dict[str, Any] | None:
    title_m = _TITLE_RE.search(chunk)
    title = _strip_tags(title_m.group(1)) if title_m else ""

    sub_m = _SUBREDDIT_RE.search(chunk)
    subreddit = sub_m.group(1) if sub_m else ""

    perm_m = _PERMALINK_RE.search(chunk)
    permalink = f"https://www.reddit.com{perm_m.group(1)}" if perm_m else ""

    time_m = _TIME_RE.search(chunk)
    created_utc = 0
    if time_m:
        try:
            iso = time_m.group(1)
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            created_utc = int(dt.timestamp())
        except ValueError:
            pass

    selftext_m = _SELFTEXT_RE.search(chunk)
    selftext = _strip_tags(selftext_m.group(1)) if selftext_m else ""

    if not (title or selftext):
        return None
    return {
        "id": post_id,
        "title": title,
        "subreddit": subreddit,
        "permalink": permalink,
        "created_utc": created_utc,
        "selftext": selftext,
    }


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def _dedupe_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out
