"""robots.txt fetch + cache + check.

Fetches /robots.txt once per origin, caches the parsed rules, and answers
can_fetch() for our declared user-agent. Status handling follows RFC 9309, on
the conservative side:
  - 200: parse the rules.
  - redirects: followed up to 5 hops, each hop through the SSRF guard.
  - 401/403: disallow all (an explicit lockout; stricter than the RFC).
  - other 4xx: no rules published, allow all.
  - 5xx, network errors, too many redirects, a blocked hop: disallow all.
The verdict is cached for the run, so a host whose robots.txt was unreachable
is skipped rather than crawled blind.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.robotparser import RobotFileParser

import httpx

from . import origin_of
from .guard import BlockedTarget, check_url

logger = logging.getLogger(__name__)

# RFC 9309: crawlers should follow at least five consecutive redirects.
MAX_ROBOTS_REDIRECTS = 5


class RobotsCache:
    def __init__(
        self, client: httpx.AsyncClient, user_agent: str, enabled: bool = True
    ) -> None:
        self._client = client
        self._ua = user_agent
        self._enabled = enabled
        self._cache: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def can_fetch(self, url: str) -> bool:
        if not self._enabled:
            return True
        rp = await self._get(origin_of(url))
        return rp.can_fetch(self._ua, url)

    async def crawl_delay(self, url: str) -> float | None:
        """The host's requested `Crawl-delay`, in seconds, or None if it asks none.

        Honouring Disallow but ignoring Crawl-delay would crawl a host asking
        for 30 s at the flat `per_host_delay_seconds`, ten times faster than
        it asked.
        """
        if not self._enabled:
            return None
        rp = await self._get(origin_of(url))
        # Only a parsed file carries directives: the allow_all / disallow_all
        # shortcuts never ran parse(), and RobotFileParser returns None there.
        delay = rp.crawl_delay(self._ua)
        if delay is None:
            return None
        try:
            value = float(delay)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def _get(self, origin: str) -> RobotFileParser:
        if origin in self._cache:
            return self._cache[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._cache:
                return self._cache[origin]
            rp = await self._fetch(origin)
            self._cache[origin] = rp
            return rp

    async def _fetch(self, origin: str) -> RobotFileParser:
        rp = RobotFileParser()
        url = f"{origin}/robots.txt"
        try:
            for _ in range(MAX_ROBOTS_REDIRECTS + 1):
                await check_url(url)
                resp = await self._client.get(url, timeout=10.0)
                if not resp.is_redirect:
                    break
                url = str(resp.url.join(resp.headers["location"]))
            else:
                logger.debug(f"robots.txt for {origin}: too many redirects; disallowing")
                rp.disallow_all = True
                return rp
        except BlockedTarget as e:
            logger.debug(f"robots.txt for {origin}: blocked hop ({e}); disallowing")
            rp.disallow_all = True
            return rp
        except Exception as e:  # noqa: BLE001 - unreachable => disallow (RFC 9309)
            logger.debug(f"robots.txt fetch failed for {origin}: {e}; disallowing")
            rp.disallow_all = True
            return rp
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        elif resp.status_code in (401, 403) or resp.status_code >= 500:
            rp.disallow_all = True  # explicit lockout, or server error
        else:
            rp.allow_all = True  # other 4xx => no rules published
        return rp
