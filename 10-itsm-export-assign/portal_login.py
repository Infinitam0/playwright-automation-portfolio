"""Sign-in and load-wait helpers for the service desk portal.

Session reuse (profile copy + browser launch) lives in shared/browser_session.py.
"""

import contextlib
import logging
import os

logger = logging.getLogger(__name__)

# Selectors, paths and UI labels are placeholders; adapt them to the target portal.
AUTHENTICATED_PATH = "/app/"  # URL path the portal serves once signed in
LOADING_TITLE = "loading"  # page title while the SPA shell is still booting
# The portal's own init function. Its first run can fail silently and leave the
# SPA stuck on its loading title; calling it again usually unblocks it.
LOADER_RETRIGGER_JS = "window.portalApp.init()"
LOGIN_LINK_LABEL = "Sign in"
SSO_OPTION_LABEL = "Continue with SSO"


async def ensure_app_loaded(page, max_retries: int = 10) -> None:
    """Wait for the portal's SPA shell to finish loading, re-triggering it when it stalls."""
    for attempt in range(1, max_retries + 1):
        await page.wait_for_timeout(2000)
        title = await page.title()
        if LOADING_TITLE not in title.lower():
            logger.info(f"App loaded (title='{title}') after {attempt} check(s).")
            return
        logger.warning(f"Page still loading (title='{title}'), re-triggering init (attempt {attempt})...")
        with contextlib.suppress(Exception):  # the call itself may throw; that's expected
            await page.evaluate(LOADER_RETRIGGER_JS)
    raise RuntimeError(f"Portal did not finish loading after {max_retries} retries.")


async def login(page, config) -> None:
    """Open the portal and click through SSO if the copied session is not enough."""
    base_url = os.environ.get("PORTAL_BASE_URL") or config.portal.base_url

    logger.info(f"Loading {base_url} ...")
    await page.goto(base_url, wait_until="commit")
    logger.info(f"URL immediately after goto: {page.url}")

    # Allow up to 3 s for any server-side redirect headers to be followed.
    await page.wait_for_timeout(3000)
    logger.info(f"URL after settle: {page.url}")

    if AUTHENTICATED_PATH in page.url:
        logger.info("Already authenticated via SSO cookies - skipping login steps.")
    else:
        login_link = page.get_by_role("link", name=LOGIN_LINK_LABEL)
        try:
            await login_link.wait_for(state="visible", timeout=15000)
            logger.info(f"'{LOGIN_LINK_LABEL}' link visible - proceeding with SSO login.")
        except Exception as e:
            raise RuntimeError(
                f"Expected the login page at {base_url} but '{LOGIN_LINK_LABEL}' was not found "
                f"and we are not on {AUTHENTICATED_PATH}. Current URL: {page.url}"
            ) from e

        logger.info(f"Clicking '{LOGIN_LINK_LABEL}'...")
        await login_link.click()

        logger.info(f"Clicking '{SSO_OPTION_LABEL}'...")
        await page.get_by_text(SSO_OPTION_LABEL, exact=True).click()

        logger.info(f"Waiting for SSO redirect to {AUTHENTICATED_PATH} ...")
        await page.wait_for_url(f"**{AUTHENTICATED_PATH}**", timeout=60000)
        logger.info(f"SSO complete. URL: {page.url}")
