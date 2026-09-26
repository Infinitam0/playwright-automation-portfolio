"""Unit tests for extractor.py — Phase 3 (EXTR-01 through EXTR-04)."""

from unittest.mock import patch

import pytest

import auth as _auth_module_ext
import extractor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_ARTICLE_HTML = """
<html><body>
  <article>
    <h1>Configuration Guide</h1>
    <p>This is the article body with enough text to meet the threshold.
    It contains multiple sentences to ensure trafilatura scores it as content
    and does not discard it as boilerplate. More words here for padding.</p>
  </article>
</body></html>
"""

TABLE_ARTICLE_HTML = """
<html><body>
  <article>
    <h1>Settings Matrix</h1>
    <table>
      <tr><th>Setting</th><th>Value</th></tr>
      <tr><td>timeout</td><td>30</td></tr>
      <tr><td>retries</td><td>3</td></tr>
    </table>
  </article>
</body></html>
"""

NAV_ONLY_HTML = """
<html><body>
  <nav><a href="/home">Home</a><a href="/about">About</a></nav>
  <footer>Copyright 2024</footer>
</body></html>
"""

ARTICLE_URL = "https://support.example.com/articles/1001"


# ---------------------------------------------------------------------------
# EXTR-01: trafilatura extraction
# ---------------------------------------------------------------------------


def test_trafilatura_extracts_markdown():
    result = extractor.extract(MINIMAL_ARTICLE_HTML, ARTICLE_URL)
    assert result, "extract() must return non-empty string for valid article HTML"
    assert "extraction failed" not in result


def test_trafilatura_returns_none_on_nav():
    """Nav-only HTML should result in a non-empty fallback or failure marker, never empty."""
    result = extractor.extract(NAV_ONLY_HTML, ARTICLE_URL)
    assert result, "extract() must not return empty string for any input"


def test_tables_preserved():
    result = extractor.extract(TABLE_ARTICLE_HTML, ARTICLE_URL)
    assert result, "extract() must handle table-heavy articles"
    # Either trafilatura or markdownify fallback should produce pipe-table syntax
    assert "|" in result, "Table-heavy article must produce markdown table syntax"


# ---------------------------------------------------------------------------
# EXTR-02: fallback behaviour
# ---------------------------------------------------------------------------


def test_fallback_on_none():
    """When trafilatura returns None, markdownify fallback must produce output."""
    with patch("extractor._trafilatura_extract", return_value=None):
        result = extractor.extract(MINIMAL_ARTICLE_HTML, ARTICLE_URL)
    assert result, "Fallback must produce non-empty result when trafilatura returns None"
    assert "extraction failed" not in result


def test_fallback_on_short_result():
    """When trafilatura returns <200 chars, fallback must fire."""
    with patch("extractor._trafilatura_extract", return_value="Too short."):
        result = extractor.extract(MINIMAL_ARTICLE_HTML, ARTICLE_URL)
    assert result
    # The markdownify fallback should produce more than 10 chars for a real article
    assert len(result) > 20


def test_failure_marker_on_missing_article():
    """When article element absent and trafilatura returns None, failure marker written."""
    with patch("extractor._trafilatura_extract", return_value=None):
        result = extractor.extract(NAV_ONLY_HTML, ARTICLE_URL)
    assert "extraction failed" in result.lower()


# ---------------------------------------------------------------------------
# EXTR-03: heading demotion
# ---------------------------------------------------------------------------


def test_demote_headings():
    md = "# Top\n## Second\n##### Level5\n###### Level6\nsome # inline"
    result = extractor.demote_headings(md)
    assert result.startswith("## Top"), f"H1 must become H2; got: {result!r}"
    assert "### Second" in result, "H2 must become H3"
    assert "###### Level5" in result, "H5 must become H6"
    assert "###### Level6" in result, "H6 must stay H6 (capped)"
    # Inline # not at line start must not be changed
    assert "some # inline" in result


# ---------------------------------------------------------------------------
# EXTR-03: table preservation via trafilatura
# ---------------------------------------------------------------------------

# (Covered by test_tables_preserved above)


# ---------------------------------------------------------------------------
# EXTR-04: header block and title stripping
# ---------------------------------------------------------------------------


def test_header_block_format():
    header = extractor.build_header(
        title="Configuration Guide",
        url=ARTICLE_URL,
        date="2000-01-02",
    )
    assert header.startswith("# Configuration Guide\n")
    assert "**Source:** " + ARTICLE_URL in header
    assert "**Scraped:** 2000-01-02" in header


def test_clean_title():
    assert extractor.clean_title("Article Name | Example Portal") == "Article Name"
    assert extractor.clean_title("Article Name - Example Portal") == "Article Name"
    assert extractor.clean_title("API: Configuration | Example Portal") == "API: Configuration"
    assert extractor.clean_title("No Suffix Here") == "No Suffix Here"


def test_scraped_date_preserved(tmp_path):
    stub = tmp_path / "abc123.md"
    stub.write_text(
        "# STUB: https://support.example.com/articles/123\n"
        "**Title:** Some Article | Example Portal\n"
        "**Scraped:** 2000-01-01T08:00:00.000000Z\n"
        "<!-- extraction pending -->\n",
        encoding="utf-8",
    )
    info = extractor.parse_stub(stub)
    assert info["scraped_date"] == "2000-01-01", (
        f"scraped_date must be YYYY-MM-DD from stub; got {info['scraped_date']!r}"
    )


# ---------------------------------------------------------------------------
# Phase 6 — Nyquist coverage: needs_extraction
# ---------------------------------------------------------------------------


def test_needs_extraction_pending(tmp_path):
    stub = tmp_path / "abc.md"
    stub.write_text("# STUB: https://example.com\n<!-- extraction pending -->\n", encoding="utf-8")
    assert extractor.needs_extraction(stub) is True


def test_needs_extraction_failed(tmp_path):
    stub = tmp_path / "abc.md"
    stub.write_text("# Title\n<!-- extraction failed: trafilatura=empty -->\n", encoding="utf-8")
    assert extractor.needs_extraction(stub) is True


def test_needs_extraction_done(tmp_path):
    stub = tmp_path / "abc.md"
    stub.write_text("# Title\n**Source:** https://example.com\n\nSome real content.\n", encoding="utf-8")
    assert extractor.needs_extraction(stub) is False


# ---------------------------------------------------------------------------
# AUTH-04 gap-closure: consecutive auth-failure abort in run_extraction_pass()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_extraction_pass_aborts_after_max_consecutive_auth_failures(tmp_path, monkeypatch):
    """run_extraction_pass() must raise RuntimeError after 3 consecutive auth failures.

    Simulated by having page.goto never leave the login URL so every extraction
    iteration sees a login-redirect page. ensure_authenticated is patched to
    no-op so the counter logic in run_extraction_pass() does the detection.
    """
    import hashlib

    import scraper as _scraper_module

    # Redirect CACHE_DIR to tmp_path
    monkeypatch.setattr(_scraper_module, "CACHE_DIR", tmp_path)

    # Create 5 fake pending stubs
    article_url_template = "https://support.example.com/articles/{i}"
    for i in range(5):
        url = article_url_template.format(i=i)
        url_hash = hashlib.md5(url.encode()).hexdigest()
        stub_path = tmp_path / f"{url_hash}.md"
        stub_path.write_text(
            f"# STUB: {url}\n**Title:** Test Article\n**Scraped:** 2000-01-04\n<!-- extraction pending -->\n",
            encoding="utf-8",
        )

    class FakePage:
        url = _auth_module_ext.LOGIN_URL  # always on login page

        async def goto(self, url):
            pass  # does NOT change self.url

        async def wait_for_load_state(self, state):
            pass

        async def content(self):
            return "<html></html>"

    class FakeContext:
        pass

    # ensure_authenticated is a no-op — counter in run_extraction_pass() does the abort
    async def fake_ensure_authenticated(page, context, username, password, target_url=None):
        pass

    monkeypatch.setattr(_auth_module_ext, "ensure_authenticated", fake_ensure_authenticated)

    # Suppress polite delay
    async def _noop_sleep(*a, **kw):
        pass

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)

    with pytest.raises(RuntimeError, match="consecutive"):
        await extractor.run_extraction_pass(FakePage(), FakeContext(), "u", "p")
