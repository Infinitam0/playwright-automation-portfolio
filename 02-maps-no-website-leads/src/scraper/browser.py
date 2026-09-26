"""Patchright persistent-context browser session (opt-in Maps adapter only).

Adapted from an earlier scraper of mine; its login flow is removed and the
realistic-browser-profile rules are kept:
  - launch_persistent_context + channel="chrome" (real Chrome)
  - headless=False, no_viewport=True
  - no UA/viewport/locale/timezone overrides (Chrome's own profile is
    self-consistent; overrides make it drift)

Patchright is imported lazily so the rest of the pipeline runs without it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_CHALLENGE_TEXT = ("verify you are human", "unusual traffic", "not a robot")


@asynccontextmanager
async def create_browser_session(profile_dir: Path):
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            "patchright is not installed. `pip install patchright && "
            "patchright install chrome` to use the Maps HTML source."
        ) from e

    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=[],
        )
        try:
            yield context
        finally:
            await context.close()


async def assert_no_challenge(page) -> None:
    """Raise ChallengeDetected if Google is pushing back.

    Two signals, cheapest first: a redirect to /sorry/ (Google's canonical block
    page) is conclusive and costs nothing to check, so it runs before pulling the
    body text.
    """
    from .errors import ChallengeDetected

    url = (page.url or "").lower()
    if "/sorry/" in url or "consent.google.com" in url and "captcha" in url:
        raise ChallengeDetected(f"Google block page: {page.url}")

    try:
        body = (await page.inner_text("body")).lower()
    except Exception:  # noqa: BLE001
        return
    if any(marker in body for marker in _CHALLENGE_TEXT):
        raise ChallengeDetected("Google served a challenge page")
