"""Patchright persistent-context browser session (opt-in Maps adapter only).

Patchright is a drop-in Playwright fork; the API below is the standard
Playwright async API. For resilience the session is a realistic, stable
browser profile rather than a synthetic one:
  - launch_persistent_context + channel="chrome" (real Chrome, kept profile)
  - headless=False, no_viewport=True
  - NO --disable-blink-features, NO UA/viewport/locale/timezone overrides
    (Chrome's native settings are self-consistent; overrides make them drift)

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
    from ..scraper.retry import ChallengeDetected

    try:
        body = (await page.inner_text("body")).lower()
    except Exception:  # noqa: BLE001
        return
    if any(marker in body for marker in _CHALLENGE_TEXT):
        raise ChallengeDetected("Google served an anti-bot challenge page")
