"""Extractor module — content extraction and markdown conversion (Phase 3)."""

import asyncio
import os
import pathlib
import random
import re
import sys

import trafilatura
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

import auth

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FALLBACK_THRESHOLD = 200  # chars — trigger markdownify if trafilatura result shorter
# Portal name appended to every browser title, e.g. "Article | Example Portal".
# Any common separator (| : - – —) before it is accepted.
PORTAL_TITLE_SUFFIX = os.getenv("PORTAL_TITLE_SUFFIX") or "Example Portal"
_TITLE_SUFFIX_RE = re.compile(rf"\s*[|:\-–—]\s*{re.escape(PORTAL_TITLE_SUFFIX)}\s*$", re.IGNORECASE)

# Portal UI artifacts rendered as spurious headings — stripped from extracted content
_STRIP_HEADINGS = {"copy link"}

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def demote_headings(md: str) -> str:
    """Shift all ATX heading levels up by one, capped at H6."""

    def _shift(m):
        level = min(len(m.group(1)) + 1, 6)
        return "#" * level + " "

    return re.sub(r"^(#{1,6}) ", _shift, md, flags=re.MULTILINE)


def clean_title(raw_title: str) -> str:
    """Strip a trailing ' | <portal name>' (any common separator) from a browser page title."""
    return _TITLE_SUFFIX_RE.sub("", raw_title).strip()


def build_header(title: str, url: str, date: str) -> str:
    """Return the standard Phase 3 header block (ends with double newline)."""
    return f"# {title}\n**Source:** {url}\n**Scraped:** {date}\n\n"


def parse_stub(cache_path: pathlib.Path) -> dict:
    """Parse a Phase 2 cache stub and return url, raw_title, scraped_date.

    Stub format (4 lines):
        # STUB: {url}
        **Title:** {title}
        **Scraped:** {ISO-timestamp}
        <!-- extraction pending -->
    """
    lines = cache_path.read_text(encoding="utf-8").splitlines()
    url = lines[0].removeprefix("# STUB: ").strip()
    raw_title = lines[1].removeprefix("**Title:** ").strip()
    scraped_raw = lines[2].removeprefix("**Scraped:** ").strip()
    scraped_date = scraped_raw[:10]  # YYYY-MM-DD — preserve Phase 2 crawl date
    return {"url": url, "raw_title": raw_title, "scraped_date": scraped_date}


# ---------------------------------------------------------------------------
# Private extraction helpers
# ---------------------------------------------------------------------------


def _preprocess(html: str) -> tuple:
    """Parse HTML, strip img and iframe tags, return (cleaned_html_str, soup).

    The cleaned_html_str is passed to trafilatura (needs string input).
    The soup object is used for the markdownify fallback (avoids re-parsing).
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["img", "iframe"]):
        tag.decompose()
    return str(soup), soup


def _trafilatura_extract(html: str):
    """Run trafilatura on cleaned HTML. Returns str or None."""
    return trafilatura.extract(
        html,
        include_tables=True,
        output_format="markdown",
    )


def _markdownify_fallback(soup: BeautifulSoup):
    """Extract article element and convert to markdown via markdownify.

    Returns None if no <article> element is present.
    """
    article = soup.select_one("article")
    if article is None:
        return None
    return MarkdownConverter(heading_style="ATX").convert_soup(article)


def _strip_artifact_headings(text: str) -> str:
    """Remove heading lines whose text matches known portal UI artifacts."""
    lines = text.splitlines(keepends=True)
    result = []
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+)", line.rstrip("\n"))
        if m and m.group(2).strip().lower() in _STRIP_HEADINGS:
            continue  # Drop the artifact heading
        result.append(line)
    return "".join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(html: str, url: str) -> str:
    """Extract article body from *html* and return clean markdown.

    Pipeline:
    1. Pre-strip: remove img and iframe from BeautifulSoup tree.
    2. trafilatura: run on cleaned HTML string.
    3. Fallback: if trafilatura returns None or <200 chars, run markdownify on <article>.
    4. Failure: if fallback also produces nothing, return failure marker string.
    5. Post-process: demote_headings() applied to body before return.

    Note: does NOT prepend the header block — caller (run_extraction_pass) does that.
    """
    cleaned_html, soup = _preprocess(html)

    # Primary: trafilatura
    body = _trafilatura_extract(cleaned_html)

    # Fallback: markdownify if trafilatura produced nothing useful
    if not body or len(body) < _FALLBACK_THRESHOLD:
        fallback = _markdownify_fallback(soup)
        if fallback and fallback.strip():
            # markdownify preserves tables and structure; prefer it over a short
            # trafilatura result whenever it produces any non-empty output
            body = fallback
        elif not body:
            # Both paths exhausted — return failure marker
            reason = "trafilatura=empty, markdownify=empty"
            return f"<!-- extraction failed: {reason} -->\n"

    return _strip_artifact_headings(demote_headings(body))


# ---------------------------------------------------------------------------
# Extraction pass helpers
# ---------------------------------------------------------------------------


def needs_extraction(cache_path: pathlib.Path) -> bool:
    """Return True if *cache_path* contains a pending or failed extraction marker."""
    try:
        content = cache_path.read_text(encoding="utf-8")
        return "<!-- extraction pending -->" in content or "<!-- extraction failed" in content
    except OSError:
        return False


async def run_extraction_pass(page, context, username: str, password: str) -> dict:
    """Iterate all cache stubs, fetch HTML via Playwright, extract, overwrite.

    - Skips stubs that are already extracted (no pending/failed marker).
    - Retries stubs marked with <!-- extraction failed -->.
    - Writes failure marker if both trafilatura and markdownify produce nothing.
    - Calls auth.ensure_authenticated() after every page navigation.
    - Applies 1–2 second polite delay between requests (matches crawl behaviour).
    """
    import scraper  # local import avoids circular dependency concern

    CACHE_DIR = scraper.CACHE_DIR
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    all_stubs = sorted(CACHE_DIR.glob("*.md"))
    pending = [p for p in all_stubs if needs_extraction(p)]

    total = len(pending)
    if total == 0:
        print("Extraction pass: no pending stubs found — all articles already extracted.")
        return {"extracted": 0, "failed": 0, "total": 0}

    print(f"Extraction pass: {total} stubs to process...")

    extracted = 0
    failed = 0
    _consecutive_auth_failures = 0
    _MAX_AUTH_FAILURES = 3

    for i, cache_path in enumerate(pending, 1):
        try:
            info = parse_stub(cache_path)
        except Exception as exc:
            print(f"WARN: could not parse stub {cache_path.name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        try:
            await page.goto(info["url"])
            await page.wait_for_load_state("networkidle")
            await auth.ensure_authenticated(page, context, username, password, target_url=info["url"])
            if auth.is_session_expired(page.url):
                _consecutive_auth_failures += 1
                if _consecutive_auth_failures >= _MAX_AUTH_FAILURES:
                    raise RuntimeError(
                        f"Aborting extraction pass: re-authentication failed {_MAX_AUTH_FAILURES} "
                        "consecutive times. Verify credentials and portal availability."
                    )
            else:
                _consecutive_auth_failures = 0

            html = await page.content()
            body = extract(html, info["url"])

            title = clean_title(info["raw_title"])
            header = build_header(title, info["url"], info["scraped_date"])

            if "extraction failed" in body:
                # extract() returned a failure marker — write header + marker
                cache_path.write_text(header + body, encoding="utf-8")
                failed += 1
                print(f"[{i}/{total}] FAILED: {info['url']}", file=sys.stderr)
            else:
                cache_path.write_text(header + body, encoding="utf-8")
                extracted += 1
                print(f"[{i}/{total}] Extracted: {info['url']}")

        except RuntimeError:
            raise  # Abort signals (e.g. consecutive auth-failure limit) must propagate

        except Exception as exc:
            # Navigation or Playwright error — write failure marker so we retry next run
            try:
                info_for_failure = parse_stub(cache_path)
                title = clean_title(info_for_failure["raw_title"])
                header = build_header(title, info_for_failure["url"], info_for_failure["scraped_date"])
                reason = f"exception: {type(exc).__name__}"
                cache_path.write_text(
                    header + f"<!-- extraction failed: {reason} -->\n",
                    encoding="utf-8",
                )
            except Exception:
                pass  # If we can't even write the failure marker, move on
            failed += 1
            print(f"WARN [{i}/{total}]: {info['url']} — {exc}", file=sys.stderr)

        await asyncio.sleep(random.uniform(1.0, 2.0))

    print(f"Extraction pass complete: {extracted} extracted, {failed} failed out of {total} pending stubs.")
    return {
        "extracted": extracted,
        "failed": failed,
        "total": total,
    }
