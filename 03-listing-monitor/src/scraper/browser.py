"""Playwright browser lifecycle, cookie consent handling, and proxy-aware sessions."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth import Stealth

from src.config import Settings
from src.scraper import site_profile as P
from src.scraper.fingerprint import FingerprintManager, FingerprintProfile

logger = logging.getLogger(__name__)

# Bot-detection mitigation: `channel="chromium"` runs the full Chromium binary in
# new headless mode rather than the headless shell, so the browser identifies
# itself like a normal desktop Chromium.
_CHANNEL = "chromium"


async def _resolve_user_agent(browser: Browser) -> str:
    """The running browser's own User-Agent with the "HeadlessChrome" marker removed.

    Derived rather than hand-written, so version and platform always match the
    installed Chromium across upgrades.
    """
    context = await browser.new_context()
    try:
        page = await context.new_page()
        ua = await page.evaluate("navigator.userAgent")
    finally:
        await context.close()
    return ua.replace("HeadlessChrome", "Chrome")


@asynccontextmanager
async def create_browser(settings: Settings) -> AsyncGenerator[Page, None]:
    """Launch a Playwright Chromium browser and yield a page.

    Applies playwright-stealth patches to reduce bot-detection false positives.
    Backward-compatible wrapper — used when proxy/fingerprint features are off.
    """
    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(
            headless=settings.headless,
            channel=_CHANNEL,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale=P.BROWSER_LOCALES[0],
            user_agent=await _resolve_user_agent(browser),
        )
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


class BrowserSession:
    """Proxy-aware browser session with fingerprint rotation.

    Playwright locks proxy settings at the context level, so rotating
    a proxy requires closing the old context and creating a new one.
    """

    def __init__(
        self,
        browser: Browser,
        fingerprint_manager: FingerprintManager,
        user_agent: str,
    ) -> None:
        self._browser = browser
        self._fingerprint_manager = fingerprint_manager
        self._user_agent = user_agent
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        """The current active page. Raises if no page has been created."""
        if self._page is None:
            raise RuntimeError("No active page — call new_page() first")
        return self._page

    async def new_page(
        self,
        proxy: dict | None = None,
        fingerprint: FingerprintProfile | None = None,
    ) -> Page:
        """Create a new browser context and page with optional proxy/fingerprint.

        Closes any existing context first.
        """
        await self._close_context()

        if fingerprint is None:
            fingerprint = self._fingerprint_manager.generate()

        context_kwargs: dict = {
            "viewport": {
                "width": fingerprint.viewport_width,
                "height": fingerprint.viewport_height,
            },
            "locale": fingerprint.locale,
            "user_agent": self._user_agent,
            "color_scheme": fingerprint.color_scheme,
            "device_scale_factor": fingerprint.device_scale_factor,
            "timezone_id": fingerprint.timezone_id,
        }

        if proxy:
            context_kwargs["proxy"] = proxy

        logger.info(
            f"New browser context: "
            f"viewport={fingerprint.viewport_width}x{fingerprint.viewport_height}, "
            f"locale={fingerprint.locale}, "
            f"proxy={'yes' if proxy else 'direct'}"
        )
        logger.debug(f"User-Agent: {self._user_agent}")

        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        return self._page

    async def rotate(self, proxy: dict | None = None) -> Page:
        """Close current context and create a new one with fresh fingerprint.

        This is how proxy rotation works with Playwright: new context = new proxy.
        """
        fingerprint = self._fingerprint_manager.generate()
        logger.info("Rotating browser context (new fingerprint + proxy)")
        return await self.new_page(proxy=proxy, fingerprint=fingerprint)

    async def _close_context(self) -> None:
        """Close the current context if one exists."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None

    async def close(self) -> None:
        """Close context and browser."""
        await self._close_context()


@asynccontextmanager
async def create_browser_session(
    settings: Settings,
) -> AsyncGenerator[BrowserSession, None]:
    """Launch a Playwright browser and yield a BrowserSession.

    Applies playwright-stealth patches and supports fingerprint/proxy rotation.
    """
    fingerprint_manager = FingerprintManager(enabled=settings.fingerprint_enabled)

    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(
            headless=settings.headless,
            channel=_CHANNEL,
            args=["--disable-blink-features=AutomationControlled"],
        )
        user_agent = await _resolve_user_agent(browser)
        logger.info(f"User-Agent: {user_agent}")
        session = BrowserSession(browser, fingerprint_manager, user_agent)
        try:
            yield session
        finally:
            await session.close()
            await browser.close()


async def dismiss_cookie_consent(page: Page) -> None:
    """Click the cookie consent accept button (site_profile.COOKIE_ACCEPT_TEXT) if present."""
    try:
        btn = page.locator("button", has_text=P.COOKIE_ACCEPT_TEXT)
        await btn.click(timeout=5000)
        logger.info("Cookie consent dismissed")
    except Exception:
        logger.debug("No cookie consent dialog found (or already dismissed)")
