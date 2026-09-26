"""Scraper module — utility layer and BFS crawl loop."""

import asyncio
import collections
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import random
import sys
from urllib.parse import urlparse, urlunparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import auth

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

VISITED_PATH = pathlib.Path("state/visited.json")
FRONTIER_PATH = pathlib.Path("state/frontier.json")
CACHE_DIR = pathlib.Path("cache")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_visited_state() -> dict:
    """Load visited state from VISITED_PATH.

    Returns {} if the file does not exist.
    On corrupt JSON or OS error, prints a warning to stderr and returns {}.
    """
    if not VISITED_PATH.exists():
        return {}
    try:
        with VISITED_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"WARNING: could not load visited state from {VISITED_PATH}: {exc}",
            file=sys.stderr,
        )
        return {}


def save_visited_state(state: dict) -> None:
    """Atomically persist *state* to VISITED_PATH.

    Writes to a .tmp file first, then uses os.replace() for an atomic rename
    that is safe on Windows (no partial-write corruption on crash).
    """
    VISITED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = VISITED_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp_path, VISITED_PATH)


def load_frontier() -> list[str]:
    """Load persisted BFS queue from FRONTIER_PATH. Returns [] if missing."""
    if not FRONTIER_PATH.exists():
        return []
    try:
        with FRONTIER_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []


def save_frontier(queue: collections.deque) -> None:
    """Atomically persist the current BFS queue to FRONTIER_PATH."""
    FRONTIER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = FRONTIER_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(list(queue), fh)
    os.replace(tmp_path, FRONTIER_PATH)


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------


def canonicalize_url(url: str) -> str:
    """Strip query parameters and URL fragments, return the bare URL.

    Returns an empty string if *url* cannot be parsed.
    """
    try:
        parsed = urlparse(url)
        clean = parsed._replace(query="", fragment="")
        return urlunparse(clean)
    except Exception:
        return ""


def is_portal_url(url: str) -> bool:
    """Return True only when *url* belongs to the support portal domain."""
    try:
        return urlparse(url).netloc == urlparse(auth.BASE_URL).netloc
    except Exception:
        return False


def is_article_url(url: str) -> bool:
    """Return True only when *url* has the support article path prefix."""
    return auth.ARTICLE_PATH in url


# ---------------------------------------------------------------------------
# Link extraction (async — requires a live Playwright page)
# ---------------------------------------------------------------------------


async def extract_links(page) -> list[str]:
    """Return all absolute href values found on *page*.

    Uses ``a.href`` (not ``getAttribute``), so the browser resolves relative
    URLs to absolute form automatically.
    """
    hrefs = await page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
    return [h for h in hrefs if h and isinstance(h, str)]


# ---------------------------------------------------------------------------
# Cache stub writing
# ---------------------------------------------------------------------------


def write_cache_stub(url: str, title: str, timestamp: str) -> None:
    """Write a cache stub markdown file for *url*.

    Filename is the 32-char hex MD5 of the canonical URL.
    Format::

        # STUB: {url}
        **Title:** {title}
        **Scraped:** {timestamp}
        <!-- extraction pending -->
    """
    url_hash = hashlib.md5(url.encode()).hexdigest()
    stub_path = CACHE_DIR / f"{url_hash}.md"
    content = f"# STUB: {url}\n**Title:** {title}\n**Scraped:** {timestamp}\n<!-- extraction pending -->\n"
    stub_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def classify_error(exc: Exception) -> str:
    """Map an exception to a short error-category string.

    Returns:
        ``"timeout"``          — Playwright timeout
        ``"http_404"``         — HTTP 404 response
        ``"navigation_error"`` — any other exception
    """
    if isinstance(exc, PlaywrightTimeoutError):
        return "timeout"
    if "404" in str(exc).lower():
        return "http_404"
    return "navigation_error"


# ---------------------------------------------------------------------------
# BFS crawl loop
# ---------------------------------------------------------------------------


async def crawl(page, context, username: str, password: str, args) -> dict:
    """BFS crawl loop — discovers all article URLs from the portal home.

    Algorithm:
    - Loads visited state on startup; skips already-visited URLs (resume support).
    - Deque-based BFS with in-memory deduplication set.
    - Writes visited.json atomically after every URL visit (crash-safe).
    - Writes a cache stub immediately on article discovery.
    - Records failed URLs with failed=True; crawl continues on error.
    - Applies a 1-2s polite delay between requests via asyncio.sleep.
    """
    # ------------------------------------------------------------------
    # Startup: load state and build in-memory visited set
    # ------------------------------------------------------------------
    state = load_visited_state()
    visited_keys = set(state.keys())
    original_visited_keys = set(state.keys())  # keys present before this session started

    if visited_keys:
        print(
            f"Starting crawl from {auth.PORTAL_HOME}... (resuming, {len(visited_keys)} URLs already visited)"
        )
    else:
        print(f"Starting crawl from {auth.PORTAL_HOME}... (fresh run)")

    # ------------------------------------------------------------------
    # Queue initialisation
    # ------------------------------------------------------------------
    start = canonicalize_url(auth.PORTAL_HOME)
    # Restore persisted frontier (crash-safe resume); fall back to seeding from start.
    saved_frontier = load_frontier()
    if saved_frontier:
        queue = collections.deque(saved_frontier)
    elif start not in visited_keys:
        queue = collections.deque([start])
    else:
        queue = collections.deque()
    # queued_keys: union of visited + already-queued to prevent duplicates this session.
    queued_keys: set[str] = set(visited_keys) | set(queue)

    # Runtime counters (for console output only — never persisted separately)
    article_count = sum(1 for v in state.values() if v.get("is_article") and not v.get("failed"))
    total_visited = len(visited_keys)

    # ------------------------------------------------------------------
    # BFS loop
    # ------------------------------------------------------------------
    _consecutive_auth_failures = 0
    _MAX_AUTH_FAILURES = 3
    while queue:
        url = queue.popleft()
        if url in visited_keys:
            continue  # Race guard: handle items enqueued before dedup

        try:
            await page.goto(url)
            await page.wait_for_load_state("networkidle")
            # Give the SPA time to render from cached data (on warm sessions it
            # renders from sessionStorage, so networkidle fires before the DOM
            # is fully populated).
            with contextlib.suppress(Exception):
                await page.wait_for_selector("a[href]", timeout=5000)
            await auth.ensure_authenticated(page, context, username, password, target_url=url)
            if auth.is_session_expired(page.url):
                _consecutive_auth_failures += 1
                if _consecutive_auth_failures >= _MAX_AUTH_FAILURES:
                    raise RuntimeError(
                        f"Aborting crawl: re-authentication failed {_MAX_AUTH_FAILURES} "
                        "consecutive times. Verify credentials and portal availability."
                    )
            else:
                _consecutive_auth_failures = 0

            # Derive post-redirect canonical URL
            canonical = canonicalize_url(page.url)
            ts = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
            is_article = is_article_url(canonical)

            # Record in state
            state[canonical] = {"visited_at": ts, "is_article": is_article}

            # Mark both pre-redirect and post-redirect URLs as visited
            visited_keys.add(canonical)
            visited_keys.add(url)

            if is_article:
                title = await page.title()
                write_cache_stub(canonical, title, ts)
                article_count += 1

            total_visited += 1
            url_path = urlparse(canonical).path
            total_known = sum(1 for v in state.values() if v.get("is_article") and not v.get("failed"))
            m_str = str(total_known) if total_known > 0 else "?"
            print(f"[{article_count}/{m_str}] Scraping: {url_path}")

            # Extract and enqueue new links
            for link in await extract_links(page):
                c = canonicalize_url(link)
                if is_portal_url(c) and c not in queued_keys and "/logout" not in c:
                    queue.append(c)
                    queued_keys.add(c)  # Enqueue-time dedup (session-only)

        except RuntimeError:
            raise  # Abort signals (e.g. consecutive auth-failure limit) must propagate

        except Exception as e:
            error_type = classify_error(e)
            ts = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
            state[url] = {
                "visited_at": ts,
                "is_article": is_article_url(url),
                "failed": True,
                "error": error_type,
            }
            visited_keys.add(url)
            total_visited += 1
            print(f"WARN: {url} — {error_type}", file=sys.stderr)

        finally:
            save_visited_state(state)
            save_frontier(queue)
            await asyncio.sleep(random.uniform(1.0, 2.0))

    # ------------------------------------------------------------------
    # Completion summary
    # ------------------------------------------------------------------
    FRONTIER_PATH.unlink(missing_ok=True)  # Clean up — crawl finished normally
    n_articles = sum(1 for v in state.values() if v.get("is_article") and not v.get("failed"))
    n_failed = sum(1 for v in state.values() if v.get("failed"))
    n_skipped = len(original_visited_keys)
    print(
        f"Crawl complete: {n_articles} articles discovered, "
        f"{n_skipped} skipped (already visited), "
        f"{n_failed} failed. Cache stubs written to cache/"
    )
    pre_existing_articles = sum(1 for k in original_visited_keys if state.get(k, {}).get("is_article"))
    return {
        "scraped": article_count,
        "skipped": pre_existing_articles,
        "failed": sum(1 for v in state.values() if v.get("failed")),
    }
