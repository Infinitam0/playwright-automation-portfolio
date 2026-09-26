"""Async Playwright browser context factory with stealth.

Browser-based scrapers (alternativeto, reddit) call `launch()` from setup()
and `close()` from teardown(). One browser process per scraper instance —
crash-isolated from the rest of the orchestrator per the design.

playwright-stealth is applied best-effort: if its API has shifted in the
installed version, we log and proceed without stealth rather than blocking
the scraper.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from scanner.logging import get_logger

log = get_logger(__name__)


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class BrowserSession:
    """Owns a Playwright + Browser + BrowserContext for the lifetime of a scrape.

    Usage:
        session = BrowserSession()
        await session.launch()
        try:
            page = await session.new_page()
            ...
        finally:
            await session.close()
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy_url: str = "",
        viewport: tuple[int, int] = (1280, 800),
    ) -> None:
        self._headless = headless
        self._user_agent = user_agent
        self._proxy_url = proxy_url
        self._viewport = viewport

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def launch(self) -> BrowserContext:
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self._headless}
        if self._proxy_url:
            launch_kwargs["proxy"] = {"server": self._proxy_url}
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            locale="en-US",
        )
        await self._apply_stealth()
        log.info("browser.launched", headless=self._headless)
        return self._context

    async def _apply_stealth(self) -> None:
        """Apply playwright-stealth if available; tolerate API drift across versions."""
        try:
            from playwright_stealth import Stealth  # type: ignore[import-untyped]
        except ImportError:
            log.info("browser.stealth_unavailable")
            return

        try:
            stealth = Stealth()
            apply = getattr(stealth, "apply_stealth_async", None) or getattr(
                stealth, "apply_async", None
            )
            if apply is None:
                log.info("browser.stealth_api_mismatch")
                return
            await apply(self._context)
            log.info("browser.stealth_applied")
        except Exception as e:  # noqa: BLE001 — stealth is best-effort
            log.warning("browser.stealth_failed", error=str(e))

    async def new_page(self):
        assert self._context is not None, "launch() must be called first"
        return await self._context.new_page()

    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, "launch() must be called first"
        return self._context

    async def close(self) -> None:
        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
            self._context = None
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None


@asynccontextmanager
async def browser_session(**kwargs):
    """Async-context wrapper. Use in tests or short scripts."""
    session = BrowserSession(**kwargs)
    await session.launch()
    try:
        yield session
    finally:
        await session.close()
