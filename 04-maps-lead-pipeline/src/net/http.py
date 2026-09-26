"""Polite async HTTP client: robots + per-host rate limit + retry/backoff.

One shared instance per pipeline run. Every crawl (website enrichment) goes
through this; the official Places API calls `post` with `check_robots=False`.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings
from ..scraper.retry import (
    ChallengeDetected,
    RetryConfig,
    retry_with_backoff,
)
from . import host_of
from .guard import check_url
from .ratelimit import PerHostRateLimiter
from .robots import RobotsCache

logger = logging.getLogger(__name__)

# Redirect chains on real sites are short; anything longer is a loop or a trap.
MAX_REDIRECTS = 5


class HttpClient:
    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout_seconds,
            # We follow redirects manually in _send so every hop can be
            # revalidated. httpx following them silently meant robots.txt was
            # checked on the pre-redirect host only.
            follow_redirects=False,
        )
        self._limiter = PerHostRateLimiter(
            settings.per_host_delay_seconds,
            settings.delay_jitter_factor,
            settings.max_concurrency,
        )
        self._robots = RobotsCache(
            self._client, settings.user_agent, settings.respect_robots
        )
        self._max_crawl_delay = settings.robots_max_crawl_delay_seconds
        self._retry = RetryConfig(
            max_retries=settings.max_retries,
            backoff_base=settings.backoff_base_seconds,
            backoff_multiplier=settings.backoff_multiplier,
            backoff_max=settings.backoff_max_seconds,
        )

    async def fetch(
        self,
        url: str,
        *,
        check_robots: bool = True,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        return await self._send(
            "GET", url, check_robots=check_robots, params=params, headers=headers
        )

    async def post(
        self, url: str, *, json: dict, headers: dict | None = None
    ) -> httpx.Response:
        # check_robots=False: the only POST target is the official Places
        # API. The SSRF guard in _send still applies.
        return await self._send("POST", url, check_robots=False, json=json, headers=headers)

    async def get_text(self, url: str, *, check_robots: bool = True) -> str:
        resp = await self.fetch(url, check_robots=check_robots)
        return resp.text

    async def _send(
        self, method: str, url: str, *, check_robots: bool = False, **kwargs
    ) -> httpx.Response:
        async def _do() -> httpx.Response:
            target = url
            # Copied per attempt: retry_with_backoff calls _do more than once and
            # the loop below drops `params` after the first hop.
            req_kwargs = dict(kwargs)
            for _ in range(MAX_REDIRECTS + 1):
                # Revalidate EVERY hop. The target site controls its own
                # redirects, so a check done once on the starting URL is no
                # check at all.
                await check_url(target)
                if check_robots and not await self._robots.can_fetch(target):
                    raise ChallengeDetected(f"robots.txt disallow: {target}")

                # Honour the host's own Crawl-delay, not just its Disallow list.
                # Beyond the cap we skip rather than crawl faster
                # than asked -- being unable to be that polite is a reason not to
                # visit, never a licence to speed up.
                delay = await self._robots.crawl_delay(target) if check_robots else None
                if delay is not None and delay > self._max_crawl_delay:
                    raise ChallengeDetected(
                        f"robots.txt Crawl-delay {delay:g}s exceeds the "
                        f"{self._max_crawl_delay:g}s cap: {target}"
                    )

                async with self._limiter.slot(host_of(target), delay):
                    resp = await self._client.request(method, target, **req_kwargs)

                # 403/429 on a crawl target is a soft-block; make it non-retryable
                # so we stop (ChallengeDetected is in the BAN_MARKERS path).
                if resp.status_code in (401, 403, 429):
                    raise ChallengeDetected(f"{resp.status_code} on {target}")
                if resp.status_code >= 500:
                    raise RuntimeError(f"{resp.status_code} server error on {target}")

                # Only GET is followed: a redirected POST needs method/body
                # rewriting that differs per status code, and the only POST here
                # goes to the Places API endpoint, which does not redirect.
                if not resp.is_redirect or method != "GET":
                    return resp
                target = str(resp.url.join(resp.headers["location"]))
                # The redirect target is absolute and carries its own query.
                req_kwargs.pop("params", None)

            raise ChallengeDetected(f"more than {MAX_REDIRECTS} redirects from {url}")

        return await retry_with_backoff(_do, self._retry)

    async def aclose(self) -> None:
        await self._client.aclose()
