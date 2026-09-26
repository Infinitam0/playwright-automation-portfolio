"""Unit tests for scraper utility layer (Plan 02-01).

Covers CRAWL-01 through OUT-05:
- canonicalize_url
- is_portal_url
- is_article_url
- load_visited_state
- save_visited_state (atomic)
- write_cache_stub
- classify_error
"""

import hashlib
import json

import pytest

import auth as _auth_module
import scraper

# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------


def test_canonicalize_url():
    """Query params and fragments are stripped."""
    url = "https://support.example.com/home?locale=en#top"
    assert scraper.canonicalize_url(url) == "https://support.example.com/home"


def test_canonicalize_url_no_change():
    """A clean URL passes through unchanged."""
    url = "https://support.example.com/articles/foo"
    assert scraper.canonicalize_url(url) == url


# ---------------------------------------------------------------------------
# is_portal_url
# ---------------------------------------------------------------------------


def test_is_portal_url():
    assert scraper.is_portal_url("https://support.example.com/home") is True
    assert scraper.is_portal_url("https://external.com/page") is False
    assert scraper.is_portal_url("") is False


# ---------------------------------------------------------------------------
# is_article_url
# ---------------------------------------------------------------------------


def test_is_article_url():
    assert scraper.is_article_url("https://support.example.com/articles/foo") is True
    assert scraper.is_article_url("https://support.example.com/home") is False


# ---------------------------------------------------------------------------
# load_visited_state
# ---------------------------------------------------------------------------


def test_load_visited_state(tmp_path, monkeypatch):
    """Returns {} when visited.json does not exist."""
    monkeypatch.setattr(scraper, "VISITED_PATH", tmp_path / "visited.json")
    result = scraper.load_visited_state()
    assert result == {}


def test_load_visited_state_corrupt(tmp_path, monkeypatch, capsys):
    """Returns {} and prints warning to stderr when JSON is corrupt."""
    visited_path = tmp_path / "visited.json"
    visited_path.write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(scraper, "VISITED_PATH", visited_path)

    result = scraper.load_visited_state()
    assert result == {}
    captured = capsys.readouterr()
    assert captured.err  # warning should go to stderr


# ---------------------------------------------------------------------------
# save_visited_state (atomic write)
# ---------------------------------------------------------------------------


def test_save_visited_state_atomic(tmp_path, monkeypatch):
    """Atomic write: final file exists, .tmp file is gone."""
    visited_path = tmp_path / "visited.json"
    monkeypatch.setattr(scraper, "VISITED_PATH", visited_path)
    # Ensure the state/ parent dir exists under tmp_path
    visited_path.parent.mkdir(parents=True, exist_ok=True)

    state = {"https://example.com": {"visited_at": "2000-01-02T10:00:00Z", "is_article": True}}
    scraper.save_visited_state(state)

    assert visited_path.exists(), "visited.json should exist after save"
    tmp_file = visited_path.with_suffix(".tmp")
    assert not tmp_file.exists(), ".tmp file should not exist after atomic replace"

    # Content should be valid JSON matching what we saved
    content = json.loads(visited_path.read_text(encoding="utf-8"))
    assert content == state


# ---------------------------------------------------------------------------
# write_cache_stub
# ---------------------------------------------------------------------------


def test_write_cache_stub(tmp_path, monkeypatch):
    """Stub file contains all four required lines."""
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path)

    url = "https://support.example.com/articles/foo"
    title = "How to configure notifications | Example Portal"
    timestamp = "2000-01-02T10:05:00Z"
    scraper.write_cache_stub(url, title, timestamp)

    url_hash = hashlib.md5(url.encode()).hexdigest()
    stub_path = tmp_path / f"{url_hash}.md"
    assert stub_path.exists(), "stub file should be created"

    content = stub_path.read_text(encoding="utf-8")
    assert f"# STUB: {url}" in content
    assert f"**Title:** {title}" in content
    assert f"**Scraped:** {timestamp}" in content
    assert "<!-- extraction pending -->" in content


def test_cache_filename_format(tmp_path, monkeypatch):
    """Filename is 32-char hex MD5 of the canonical URL."""
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path)

    url = "https://support.example.com/articles/bar"
    scraper.write_cache_stub(url, "Bar title", "2000-01-02T10:00:00Z")

    expected_hash = hashlib.md5(url.encode()).hexdigest()
    assert len(expected_hash) == 32
    expected_filename = f"{expected_hash}.md"
    assert (tmp_path / expected_filename).exists()


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


def test_classify_error():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    timeout_exc = PlaywrightTimeoutError("Timeout exceeded")
    assert scraper.classify_error(timeout_exc) == "timeout"

    not_found_exc = Exception("Navigation error: 404 not found")
    assert scraper.classify_error(not_found_exc) == "http_404"

    generic_exc = Exception("Something went wrong")
    assert scraper.classify_error(generic_exc) == "navigation_error"


# ---------------------------------------------------------------------------
# Smoke: crawl() exists in scraper module (full impl deferred to Plan 02)
# ---------------------------------------------------------------------------


def test_crawl_exists():
    """crawl() is present in the scraper module (implemented in Plan 02)."""
    assert hasattr(scraper, "crawl"), "scraper module must expose crawl()"
    assert callable(scraper.crawl)


# ---------------------------------------------------------------------------
# DX-01 — progress format (Phase 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_format(tmp_path, monkeypatch, capsys):
    """crawl() prints [N/M] Scraping: {url_path} format per DX-01."""
    # Monkeypatch filesystem paths so test is isolated
    monkeypatch.setattr(scraper, "VISITED_PATH", tmp_path / "visited.json")
    monkeypatch.setattr(scraper, "FRONTIER_PATH", tmp_path / "frontier.json")
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()

    # Stub page and context: navigate to one article URL then stop
    article_url = "https://support.example.com/articles/12345"

    class FakePage:
        url = article_url

        async def goto(self, url):
            pass

        async def wait_for_load_state(self, state):
            pass

        async def wait_for_selector(self, sel, timeout=None):
            pass

        async def title(self):
            return "Test Article | Example Portal"

        async def evaluate(self, expr):
            return []  # no links — stops crawl

    class FakeContext:
        pass

    # Prevent polite delay from blocking test
    async def _noop_sleep(*a, **kw):
        pass

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)

    await scraper.crawl(FakePage(), FakeContext(), "u", "p", type("A", (), {"force": False})())

    captured = capsys.readouterr()
    # Must contain [N/M] format
    assert "[" in captured.out and "] Scraping:" in captured.out, (
        f"Expected [N/M] Scraping: format, got: {captured.out!r}"
    )
    assert "/articles/12345" in captured.out


# ---------------------------------------------------------------------------
# Phase 6 — Nyquist coverage: load_frontier / save_frontier
# ---------------------------------------------------------------------------


def test_save_load_frontier(tmp_path, monkeypatch):
    import collections

    monkeypatch.setattr(scraper, "FRONTIER_PATH", tmp_path / "frontier.json")
    q = collections.deque(["https://example.com/a", "https://example.com/b"])
    scraper.save_frontier(q)
    loaded = scraper.load_frontier()
    assert loaded == list(q)


def test_load_frontier_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "FRONTIER_PATH", tmp_path / "frontier.json")
    assert scraper.load_frontier() == []


# ---------------------------------------------------------------------------
# AUTH-04 gap-closure: consecutive auth-failure abort in crawl() (Plan 05-02)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawl_aborts_after_max_consecutive_auth_failures(tmp_path, monkeypatch):
    """crawl() must raise RuntimeError after 3 consecutive auth failures.

    Simulated by having page.goto never leave the login URL so every iteration
    of the BFS loop sees a login-redirect URL, triggering the consecutive-failure
    counter. ensure_authenticated is patched to no-op so the counter logic in
    crawl() (not ensure_authenticated) is what aborts.
    """
    monkeypatch.setattr(scraper, "VISITED_PATH", tmp_path / "visited.json")
    monkeypatch.setattr(scraper, "FRONTIER_PATH", tmp_path / "frontier.json")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(scraper, "CACHE_DIR", cache_dir)

    class FakePage:
        url = _auth_module.LOGIN_URL  # always returns login URL

        async def goto(self, url):
            pass  # does NOT change self.url

        async def wait_for_load_state(self, state):
            pass

        async def wait_for_selector(self, sel, timeout=None):
            pass

        async def evaluate(self, expr):
            return []  # no links extracted

        async def title(self):
            return "Test"

        async def content(self):
            return "<html></html>"

    class FakeContext:
        pass

    # ensure_authenticated is a no-op so the counter in crawl() can detect the condition
    async def fake_ensure_authenticated(page, context, username, password, target_url=None):
        pass

    monkeypatch.setattr(_auth_module, "ensure_authenticated", fake_ensure_authenticated)

    # Suppress polite delay
    async def _noop_sleep(*a, **kw):
        pass

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)

    # Seed queue with 5 URLs so the loop has enough iterations to hit the 3-failure limit
    seed_urls = [f"https://support.example.com/page{i}" for i in range(5)]
    # Write a fake frontier so crawl() picks up our seed URLs
    import json as _json

    (tmp_path / "frontier.json").write_text(_json.dumps(seed_urls), encoding="utf-8")

    args = type("A", (), {"force": False})()

    with pytest.raises(RuntimeError, match="consecutive"):
        await scraper.crawl(FakePage(), FakeContext(), "u", "p", args)


# ---------------------------------------------------------------------------
# Re-login mid-crawl must return to the requested page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawl_relogin_returns_to_requested_article(tmp_path, monkeypatch):
    """After an expired session is re-authenticated, the article is recorded under its
    own URL, not under the post-login landing page."""
    monkeypatch.setattr(scraper, "VISITED_PATH", tmp_path / "visited.json")
    monkeypatch.setattr(scraper, "FRONTIER_PATH", tmp_path / "frontier.json")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(scraper, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(_auth_module, "SESSION_PATH", tmp_path / "session.json")

    article_url = f"{_auth_module.BASE_URL}{_auth_module.ARTICLE_PATH}42"
    (tmp_path / "frontier.json").write_text(json.dumps([article_url]), encoding="utf-8")

    class FakePage:
        url = ""
        session_expired = True

        async def goto(self, url):
            # First navigation hits an expired session and is redirected to login.
            self.url = _auth_module.LOGIN_URL if self.session_expired else url

        async def wait_for_load_state(self, state):
            pass

        async def wait_for_selector(self, sel, timeout=None):
            pass

        async def evaluate(self, expr):
            return []

        async def title(self):
            return "Article 42"

    class FakeContext:
        async def storage_state(self, path):
            pass

    async def fake_perform_login(page, context, username, password):
        page.session_expired = False
        page.url = _auth_module.PORTAL_HOME  # portal lands on its home page after login

    async def _noop_sleep(*a, **kw):
        pass

    monkeypatch.setattr(_auth_module, "_perform_login", fake_perform_login)
    monkeypatch.setattr("asyncio.sleep", _noop_sleep)

    await scraper.crawl(FakePage(), FakeContext(), "u", "p", type("A", (), {"force": False})())

    state = json.loads((tmp_path / "visited.json").read_text(encoding="utf-8"))
    assert state.get(article_url, {}).get("is_article") is True
    assert _auth_module.PORTAL_HOME not in state
    assert (cache_dir / f"{hashlib.md5(article_url.encode()).hexdigest()}.md").exists()
