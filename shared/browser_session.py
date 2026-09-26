"""Reuse the operator's own signed-in Edge/Chrome session in Playwright.

The bot runs on the operator's machine, as the operator: it copies the
operator's own browser profile into a temp directory and launches a persistent
context on the copy, so it starts out signed in to the portals the operator
already uses. The real profile is never modified (the browser may even stay
open while the bot runs). Use it only with your own account and with
permission to automate the target portal.
"""

import logging
import os
import shutil
import sqlite3
import tempfile

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# Profile files and directories that hold the browser's site storage.
PROFILE_FILES = ["Cookies", "Preferences", "Secure Preferences"]
PROFILE_DIRS = [
    "Local Storage",
    "Network",  # modern Chromium keeps its cookie DB in Network/Cookies
    "IndexedDB",
    "Session Storage",
    "Service Worker",
]


def default_user_data_dir(channel: str = "msedge") -> str:
    """Default Windows user data dir for Edge (msedge) or Chrome (chrome)."""
    vendor = ("Microsoft", "Edge") if channel == "msedge" else ("Google", "Chrome")
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), *vendor, "User Data")


def _copy_ignore_locked(src: str, dst: str) -> None:
    """Like shutil.copy2 but skips files locked by the running browser."""
    try:
        shutil.copy2(src, dst)
    except PermissionError:
        logger.warning(f"Skipped locked file: {os.path.basename(src)}")


def _copy_sqlite_safe(src_path: str, dst_path: str) -> bool:
    """Copy a SQLite database via the backup API; works while the browser holds it open."""
    try:
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        dst = sqlite3.connect(dst_path)
        src.backup(dst)
        dst.close()
        src.close()
        return True
    except Exception as e:
        logger.warning(f"SQLite backup failed for {os.path.basename(src_path)}: {e}")
        return False


def _copytree_skip_locks(src: str, dst: str) -> None:
    """Copy a LevelDB/directory tree, skipping LOCK files and tolerating locked files."""
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("LOCK"),
        copy_function=_copy_ignore_locked,
        ignore_dangling_symlinks=True,
    )


def copy_browser_profile(user_data_dir: str | None = None, profile: str = "Default", channel: str = "msedge") -> str:
    """Copy the session files of a browser profile to a temp dir and return its path.

    An empty *user_data_dir* means the default user data dir of *channel*.
    """
    src_root = user_data_dir or default_user_data_dir(channel)
    src_profile = os.path.join(src_root, profile)
    if not os.path.isdir(src_profile):
        raise FileNotFoundError(
            f"Browser profile directory not found: {src_profile}\n"
            "Check the browser user_data_dir / profile_name settings."
        )

    # Local State lives at the root and is required for Chromium to recognise
    # the profile. The copy only works for the same Windows user on the same
    # machine (Chromium protects its storage with DPAPI, which is bound to that
    # user), which is why this is limited to reusing your own session.
    local_state_src = os.path.join(src_root, "Local State")
    if not os.path.exists(local_state_src):
        raise FileNotFoundError(f"Local State not found: {local_state_src}")

    temp_dir = tempfile.mkdtemp(prefix="browser_profile_")
    logger.info(f"Temp profile dir: {temp_dir}")
    shutil.copy2(local_state_src, os.path.join(temp_dir, "Local State"))

    temp_profile = os.path.join(temp_dir, profile)
    os.makedirs(temp_profile, exist_ok=True)

    for fname in PROFILE_FILES:
        src = os.path.join(src_profile, fname)
        if os.path.exists(src):
            _copy_ignore_locked(src, os.path.join(temp_profile, fname))

    for dname in PROFILE_DIRS:
        src = os.path.join(src_profile, dname)
        if os.path.exists(src):
            _copytree_skip_locks(src, os.path.join(temp_profile, dname))
            logger.debug(f"Copied {dname}")

    # The browser locks Network/Cookies while running, so the plain copy above
    # skips it. Fall back to a SQLite hot-backup of the cookie DB.
    cookies_src = os.path.join(src_profile, "Network", "Cookies")
    cookies_dst = os.path.join(temp_profile, "Network", "Cookies")
    if os.path.exists(cookies_src) and not os.path.exists(cookies_dst):
        logger.info("Network/Cookies was locked - attempting SQLite hot-backup...")
        if _copy_sqlite_safe(cookies_src, cookies_dst):
            logger.info("Network/Cookies copied via SQLite backup API.")

    return temp_dir


async def launch_browser(
    temp_dir: str,
    headless: bool = False,
    channel: str = "msedge",
    timeout_ms: int | None = None,
):
    """Launch a persistent context on the copied profile. Returns (playwright, context, page)."""
    pw = await async_playwright().start()
    logger.info(f"Launching {channel} with persistent context...")
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=temp_dir,
        channel=channel,
        headless=headless,
        args=[
            "--start-maximized",
            "--no-first-run",
            "--disable-features=msEdgeFirstRunExperience",
        ],
        no_viewport=True,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    if timeout_ms:
        page.set_default_timeout(timeout_ms)
    return pw, context, page


async def safe_goto(page, url: str, max_retries: int = 3) -> None:
    """page.goto with retry on net::ERR_ABORTED (an SSO redirect interrupting the load)."""
    for attempt in range(max_retries):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as e:
            if "net::ERR_ABORTED" in str(e) and attempt < max_retries - 1:
                logger.warning(f"ERR_ABORTED on attempt {attempt + 1}, retrying...")
                await page.wait_for_load_state("load")
                continue
            raise


async def _appears(locator, timeout_ms: int) -> bool:
    """True if the element becomes visible within the timeout (is_visible() doesn't wait)."""
    try:
        await locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


async def sso_login(
    page,
    entry_url: str,
    sso_button_name: str,
    account_name: str,
    warmup_url: str,
) -> None:
    """Pass through a portal's "Sign in with SSO" page using the copied session.

    Clicks the SSO button and the account tile when they are shown (with a warm
    session they usually are not). Many portals sign in on one domain and serve
    the app from another, so a final visit to *warmup_url* establishes the
    app-domain session before any real work starts.
    """
    logger.info(f"Navigating to: {entry_url}")
    await page.goto(entry_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    sso_button = page.get_by_role("button", name=sso_button_name)
    try:
        if await _appears(sso_button, 5000):
            logger.info(f"Clicking SSO button: {sso_button_name}")
            await sso_button.click()
            await page.wait_for_timeout(3000)

            # Account picker, shown by some identity providers
            account_button = page.get_by_role("button", name=account_name)
            if await _appears(account_button, 5000):
                logger.info("Selecting account tile")
                await account_button.click()
                await page.wait_for_timeout(5000)
        else:
            logger.info("SSO button not visible - may already be authenticated")
    except Exception as e:
        logger.info(f"SSO flow skipped: {e} - assuming already logged in")

    await page.wait_for_load_state("networkidle", timeout=30000)
    logger.info("SSO login completed")

    logger.info(f"Warming up app session: {warmup_url}")
    await safe_goto(page, warmup_url)
    await page.wait_for_timeout(2000)
    logger.info(f"Session established on: {page.url}")


def cleanup_profile(temp_dir: str | None) -> None:
    """Remove the temporary profile directory."""
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"Cleaned up temp profile: {temp_dir}")
