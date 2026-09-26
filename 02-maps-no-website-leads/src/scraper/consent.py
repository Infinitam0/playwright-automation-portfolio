"""EU consent interstitial handling, and the one navigation helper everything uses.

Discovered the hard way: pinning `hl=en&gl=us` does NOT dodge the consent wall —
Google keys it off the request IP, not the URL params. From a European IP every
single navigation lands on `consent.google.com/m` with the real page behind it,
and `APP_INITIALIZATION_STATE` is simply absent. Left unhandled this looks
exactly like "extraction is broken".

Pinning `hl=en` is still worth it: it forces the consent buttons into English, so
matching on "Reject all" is reliable rather than a guessing game across locales.

The persistent Chrome profile means this is paid once per profile, not per page.
"""

from __future__ import annotations

import logging

from . import selectors as S
from .browser import assert_no_challenge

logger = logging.getLogger(__name__)

_CONSENT_HOST = "consent.google.com"


def _with_locale(url: str) -> str:
    if "hl=" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}{S.LOCALE_PARAMS}"


async def _is_consent_page(page) -> bool:
    return _CONSENT_HOST in (page.url or "")


async def handle_consent(page, *, timeout_ms: int = 15_000) -> bool:
    """Dismiss the consent wall if present. Returns True if it acted.

    Rejects rather than accepts — same practical outcome for us, better posture.
    """
    if not await _is_consent_page(page):
        return False

    logger.info("consent wall hit, rejecting")

    for text in S.CONSENT_REJECT_TEXTS:
        try:
            btn = page.locator(f'{S.CONSENT_FORM} button:has-text("{text}")').first
            if await btn.count() == 0:
                continue
            await btn.click(timeout=5_000)
            await page.wait_for_url(
                lambda u: _CONSENT_HOST not in u, timeout=timeout_ms
            )
            logger.info("consent cleared via %r", text)
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("consent button %r did not work: %s", text, e)
            continue

    # Fallback: submit the reject form directly. The page ships two forms with
    # distinct jsaction handlers; the reject one is whichever holds that button.
    try:
        await page.evaluate(
            """() => {
              for (const f of document.querySelectorAll('form[action*="consent.google"]')) {
                const b = [...f.querySelectorAll('button')]
                  .find(x => /reject|afwijzen|ablehnen|refuser|rechazar/i.test(x.textContent));
                if (b) { b.click(); return true; }
              }
              return false;
            }"""
        )
        await page.wait_for_url(lambda u: _CONSENT_HOST not in u, timeout=timeout_ms)
        logger.info("consent cleared via form fallback")
        return True
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not dismiss the consent wall: {e}") from e


async def safe_goto(page, url: str, *, timeout_ms: int = 45_000) -> None:
    """Navigate, clear consent if it appears, then check for a block page.

    Every navigation in this project goes through here. Consent must be cleared
    before assert_no_challenge, or the consent page's own text trips the check.
    """
    await page.goto(_with_locale(url), wait_until="domcontentloaded", timeout=timeout_ms)

    if await handle_consent(page):
        # The redirect back can drop our params; re-navigate to be certain we
        # land on the requested page with the locale pinned.
        if _CONSENT_HOST in page.url or "/maps/" not in page.url:
            await page.goto(
                _with_locale(url), wait_until="domcontentloaded", timeout=timeout_ms
            )

    await assert_no_challenge(page)
