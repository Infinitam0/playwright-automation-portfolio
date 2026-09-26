"""Auth module — login and session management for an authenticated support portal."""

import os
import pathlib
import re

from dotenv import load_dotenv

# Load .env at import so PORTAL_BASE_URL is known before the URL constants are built.
load_dotenv()

SESSION_PATH = pathlib.Path(".auth/session.json")

# Portal URL layout. Neutral defaults; override via env to match the target portal.
BASE_URL = (os.getenv("PORTAL_BASE_URL") or "https://support.example.com").rstrip("/")
LOGIN_PATH = os.getenv("PORTAL_LOGIN_PATH") or "/login"
HOME_PATH = os.getenv("PORTAL_HOME_PATH") or "/home"
ARTICLE_PATH = os.getenv("PORTAL_ARTICLE_PATH") or "/articles/"
CATEGORY_PATH = os.getenv("PORTAL_CATEGORY_PATH") or "/categories/"

LOGIN_PATTERNS = ["/login", "/signin", LOGIN_PATH]  # URL patterns that indicate a login redirect
LOGIN_URL = f"{BASE_URL}{LOGIN_PATH}"
PORTAL_HOME = f"{BASE_URL}{HOME_PATH}"

# Accessible-name patterns for the login form fields (role-based locators).
# Adjust these to the portal's UI language if its labels are not in English.
EMAIL_LABEL = re.compile(r"e-?mail", re.IGNORECASE)
PASSWORD_LABEL = re.compile(r"password", re.IGNORECASE)


def get_credentials() -> tuple[str, str]:
    """Load and return (username, password) from .env. Raises SystemExit if missing."""
    load_dotenv()
    username = os.getenv("PORTAL_USERNAME")
    password = os.getenv("PORTAL_PASSWORD")
    if not username or not password:
        raise SystemExit(
            "ERROR: PORTAL_USERNAME and PORTAL_PASSWORD must be set in .env\n"
            "Copy .env.example to .env and fill in your credentials."
        )
    return username, password


def is_session_expired(url: str) -> bool:
    """Return True if the given URL indicates a redirect to the login page."""
    return any(pattern in url for pattern in LOGIN_PATTERNS)


async def _perform_login(page, context, username: str, password: str):
    """Navigate existing page to LOGIN_URL, fill login form.

    Navigates in-place — no new page created. Raises RuntimeError on bad credentials.
    """
    await page.goto(LOGIN_URL)
    await page.wait_for_load_state("networkidle")

    # Selectors discovered via Playwright codegen on the live portal
    await page.get_by_role("textbox", name=EMAIL_LABEL).fill(username)
    await page.get_by_role("textbox", name=PASSWORD_LABEL).fill(password)
    # Submit button — type=submit targets the form submit button reliably
    await page.locator("button[type='submit']").first.click()
    await page.wait_for_load_state("networkidle")

    if is_session_expired(page.url):
        raise RuntimeError("Login failed: verify PORTAL_USERNAME and PORTAL_PASSWORD in .env")


async def login(browser, username: str, password: str):
    """Perform Playwright login and persist session.

    Returns (context, page) — the authenticated context and an open page
    pointing to the portal. Saves Playwright storage state to SESSION_PATH.
    On failure, writes auth_failure_trace.zip and raises RuntimeError.
    """
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)

    storage_state_path = str(SESSION_PATH) if SESSION_PATH.exists() else None
    context = await browser.new_context(storage_state=storage_state_path)
    await context.tracing.start(screenshots=True, snapshots=True)

    try:
        if SESSION_PATH.exists():
            # Verify existing session is still valid
            page = await context.new_page()
            await page.goto(PORTAL_HOME)
            await page.wait_for_load_state("networkidle")
            if is_session_expired(page.url):
                # Session expired — re-authenticate in-place on the existing page
                SESSION_PATH.unlink(missing_ok=True)
                await _perform_login(page, context, username, password)
        else:
            page = await context.new_page()
            await _perform_login(page, context, username, password)

        # Persist the authenticated session state
        await context.storage_state(path=str(SESSION_PATH))
        # Discard trace on success
        await context.tracing.stop()

    except Exception as e:
        await context.tracing.stop(path="auth_failure_trace.zip")
        raise RuntimeError(f"Auth failed: {e}") from e

    return (context, page)


async def ensure_authenticated(
    page, context, username: str, password: str, target_url: str | None = None
) -> None:
    """Re-authenticate if session has expired; no-op if still authenticated.

    After a re-login the portal lands on its post-login page, so when *target_url*
    is given the page is navigated back to it.
    """
    if is_session_expired(page.url):
        SESSION_PATH.unlink(missing_ok=True)
        await _perform_login(page, context, username, password)
        if is_session_expired(page.url):
            raise RuntimeError(
                "Re-authentication failed: still on login page after _perform_login. "
                "Check credentials or portal availability."
            )
        await context.storage_state(path=str(SESSION_PATH))
        if target_url:
            await page.goto(target_url)
            await page.wait_for_load_state("networkidle")
