"""Unit tests for auth module — AUTH-01 (missing credentials), AUTH-04 (session expiry)."""

import pytest

import auth


def test_credentials_missing(monkeypatch):
    # Patch load_dotenv to prevent .env file from overriding deleted env vars
    monkeypatch.setattr("auth.load_dotenv", lambda: None)
    monkeypatch.delenv("PORTAL_USERNAME", raising=False)
    monkeypatch.delenv("PORTAL_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        auth.get_credentials()
    msg = str(exc_info.value)
    assert "PORTAL_USERNAME" in msg
    assert "PORTAL_PASSWORD" in msg


def test_credentials_present(monkeypatch):
    # Patch load_dotenv to prevent .env file from interfering with test values
    monkeypatch.setattr("auth.load_dotenv", lambda: None)
    monkeypatch.setenv("PORTAL_USERNAME", "user@example.com")
    monkeypatch.setenv("PORTAL_PASSWORD", "secret")
    result = auth.get_credentials()
    assert result == ("user@example.com", "secret")


def test_session_expiry_detection():
    assert auth.is_session_expired("/login") is True
    assert auth.is_session_expired("/articles/foo") is False
    assert auth.is_session_expired("/help/signin") is True


async def test_ensure_authenticated_navigates_existing_page(monkeypatch):
    """AUTH-04: ensure_authenticated must navigate the existing page object in-place,
    not discard it in favour of a new page created inside _perform_login."""

    class FakePage:
        def __init__(self):
            self.url = "/login"  # starts at login URL — triggers is_session_expired

        async def goto(self, url):
            self.url = url

        async def wait_for_load_state(self, state):
            pass

    class FakeContext:
        pass

    fake_page = FakePage()
    fake_context = FakeContext()

    # Monkeypatch _perform_login to a coroutine that navigates the EXISTING page in-place
    # (mirroring what the fixed implementation will do)
    async def fake_perform_login(page, context, username, password):
        await page.goto(auth.PORTAL_HOME)

    monkeypatch.setattr(auth, "_perform_login", fake_perform_login)

    # Also monkeypatch storage_state to avoid I/O
    async def fake_storage_state(path):
        pass

    fake_context.storage_state = fake_storage_state

    await auth.ensure_authenticated(fake_page, fake_context, "u", "p")

    # After ensure_authenticated, the existing page must NOT be at a login URL
    assert not auth.is_session_expired(fake_page.url), (
        f"Expected page to be navigated away from login URL, but url is still: {fake_page.url}"
    )


# ---------------------------------------------------------------------------
# AUTH-04 gap-closure: /login pattern detection (Plan 05-02)
# ---------------------------------------------------------------------------


def test_session_expiry_detects_support_login():
    """is_session_expired must recognise /login as a login page."""
    assert auth.is_session_expired("/login") is True
    assert auth.is_session_expired("https://support.example.com/login") is True
    # Sanity: non-login URLs must still return False
    assert auth.is_session_expired("/articles/foo") is False


async def test_ensure_authenticated_raises_on_silent_login_failure(monkeypatch):
    """ensure_authenticated must raise RuntimeError when _perform_login does not
    navigate away from the login page (silent failure / broken session)."""

    class FakePage:
        def __init__(self):
            self.url = "/login"  # stays on login page permanently

        async def goto(self, url):
            pass  # intentionally does NOT update self.url

        async def wait_for_load_state(self, state):
            pass

    class FakeContext:
        async def storage_state(self, path):
            pass

    fake_page = FakePage()
    fake_context = FakeContext()

    # _perform_login is a no-op: page stays at /login
    async def fake_perform_login(page, context, username, password):
        pass  # does not navigate away — simulates broken credentials / portal

    monkeypatch.setattr(auth, "_perform_login", fake_perform_login)

    with pytest.raises(RuntimeError, match="still on login page"):
        await auth.ensure_authenticated(fake_page, fake_context, "u", "p")
