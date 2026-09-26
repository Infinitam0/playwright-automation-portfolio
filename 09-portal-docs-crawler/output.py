"""Output module — markdown assembly and consolidation (Phase 4)."""

import datetime
import os
import pathlib
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

import auth as auth_module
import extractor
import scraper  # for VISITED_PATH, CACHE_DIR, load_visited_state

SPLIT_THRESHOLD = 350_000  # words


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Generate URL-safe slug from a category name (handles accented characters)."""
    normalized = unicodedata.normalize("NFD", name)
    ascii_only = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


def heading_anchor(title: str) -> str:
    """Generate GFM-compatible heading anchor from title text."""
    lowered = title.lower()
    no_special = re.sub(r"[^\w\s-]", "", lowered)
    return re.sub(r"[\s_]+", "-", no_special).strip("-")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_atomic(path: pathlib.Path, content: str) -> None:
    """Write content to path atomically via .tmp + os.replace()."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _read_articles(cache_dir: pathlib.Path) -> tuple:
    """Read all cache .md files. Returns (articles, failed_count).

    Each article dict: {title, url, cache_path, content_body}
    Skips files with extraction failed/pending markers (counts as failed).
    title = first line of file stripped of "# "
    url = second line stripped of "**Source:** "
    """
    articles = []
    failed_count = 0

    for md_file in sorted(cache_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            failed_count += 1
            continue

        # Skip failed/pending stubs
        if "<!-- extraction failed" in text or "<!-- extraction pending" in text:
            failed_count += 1
            continue

        lines = text.splitlines()
        if not lines:
            failed_count += 1
            continue

        # Parse title (first line: "# Article Title")
        title = lines[0].lstrip("# ").strip() if lines else ""

        # Parse URL (second line: "**Source:** https://...")
        url = ""
        if len(lines) > 1:
            source_line = lines[1]
            if source_line.startswith("**Source:**"):
                url = source_line[len("**Source:**") :].strip()

        # Body is everything after the header block (title, source, scraped, blank line)
        # Find the blank line after the header
        body_start = 0
        for i, line in enumerate(lines):
            if i >= 3 and line.strip() == "":
                body_start = i + 1
                break
        if body_start == 0:
            # Fallback: everything after line 4
            body_start = min(4, len(lines))

        content_body = "\n".join(lines[body_start:])

        articles.append(
            {
                "title": title,
                "url": url,
                "cache_path": md_file,
                "content_body": content_body,
            }
        )

    return articles, failed_count


async def _build_category_map(page, context, username: str, password: str) -> dict:
    """Navigate each CATEGORY_PATH{id} page in visited.json and return {article_url: category_name}.

    Strategy:
    - Load visited.json
    - Filter entries where is_article=False and URL ends in CATEGORY_PATH + digits (top-level only)
    - Navigate to each, get page title (strip portal suffix), collect article links on that page
    - Map each article URL found on that page -> category name
    - First-category-wins for articles appearing under multiple categories
    - If page errors: skip, those articles become Uncategorized
    - If page is None or not a real Playwright page (e.g. in tests): return {}
    """
    # If page is a mock/None (test scenario), return empty map
    if page is None:
        return {}

    # Check if we have a real Playwright page by checking for async goto
    try:
        visited = scraper.load_visited_state()
    except Exception:
        return {}

    category_map: dict = {}

    # Filter for top-level category pages (not sub-category pages)
    category_pattern = re.compile(re.escape(auth_module.CATEGORY_PATH) + r"(\d+)$")

    category_urls = [
        url
        for url, data in visited.items()
        if not data.get("is_article", True) and category_pattern.search(url)
    ]

    try:
        for category_url in category_urls:
            try:
                await auth_module.ensure_authenticated(page, context, username, password)
                await page.goto(category_url)
                await page.wait_for_load_state("networkidle")

                # Get category name from page title
                title = await page.title()
                # Strip portal suffix (e.g., " | Example Portal")
                category_name = extractor.clean_title(title)
                if not category_name:
                    continue

                # Collect article links on this page
                links = await page.query_selector_all(f"a[href*='{auth_module.ARTICLE_PATH}']")
                for link in links:
                    href = await link.get_attribute("href")
                    if href:
                        # Normalize URL
                        if href.startswith("/"):
                            href = auth_module.BASE_URL + href
                        # Strip query/fragment
                        parsed = urlparse(href)
                        canonical = urlunparse(parsed._replace(query="", fragment=""))
                        if canonical not in category_map:
                            category_map[canonical] = category_name

            except Exception:
                # Skip this category page on any error
                continue

    except Exception:
        return {}

    return category_map


def _assemble_combined(articles_by_category: dict, generated: str, total: int) -> str:
    """Build the full combined markdown string.

    Prints '[N/M] Writing: {title}' to stdout for each article as it is appended.
    N is a running counter (1-based), M is total article count across all categories.

    Structure:
    # Support Portal Documentation
    **Generated:** {date} | **Articles:** {count}

    ## Table of Contents
    - [Category Name](#category-name)
    ...

    ---

    # Category Name

    ## Article Title
    **Source:** {url}
    ...article body...

    ---

    # Next Category
    ...
    """
    parts = []

    # Document header
    parts.append("# Support Portal Documentation\n")
    parts.append(f"**Generated:** {generated} | **Articles:** {total}\n\n")

    # Table of Contents (categories only)
    parts.append("## Table of Contents\n\n")
    for cat in articles_by_category:
        anchor = heading_anchor(cat)
        parts.append(f"- [{cat}](#{anchor})\n")
    parts.append("\n---\n\n")

    # Article content grouped by category
    n = 1
    for cat, articles in articles_by_category.items():
        parts.append(f"# {cat}\n\n")
        for art in articles:
            print(f"[{n}/{total}] Writing: {art['title']}")
            # Article as H2 section
            parts.append(f"## {art['title']}\n")
            parts.append(f"**Source:** {art['url']}\n\n")
            if art["content_body"].strip():
                parts.append(art["content_body"].rstrip())
                parts.append("\n\n")
            n += 1
        parts.append("---\n\n")

    return "".join(parts)


def _assemble_category_file(cat: str, articles: list, generated: str) -> str:
    """Build a per-category markdown file for split mode.

    Each file has:
    - H1 category header
    - Generated date and article count
    - Article-level TOC (all article titles with anchors within this file)
    - Article sections as H2
    """
    parts = []

    count = len(articles)

    # Header
    parts.append(f"# {cat}\n")
    parts.append(f"**Generated:** {generated} | **Articles:** {count}\n\n")

    # Article-level TOC
    parts.append("## Table of Contents\n\n")
    for art in articles:
        anchor = heading_anchor(art["title"])
        parts.append(f"- [{art['title']}](#{anchor})\n")
    parts.append("\n---\n\n")

    # Article sections
    for art in articles:
        parts.append(f"## {art['title']}\n")
        parts.append(f"**Source:** {art['url']}\n\n")
        if art["content_body"].strip():
            parts.append(art["content_body"].rstrip())
            parts.append("\n\n")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def assemble(
    cache_dir: pathlib.Path,
    output_dir: pathlib.Path,
    page,
    context,
    username: str,
    password: str,
) -> dict:
    """Assemble cached articles into combined markdown output.

    Returns: {word_count: int, output_files: list[str], article_count: int, failed_count: int}
    """
    articles, failed_count = _read_articles(cache_dir)
    category_map = await _build_category_map(page, context, username, password)

    # Group by category
    categories: dict = {}
    for art in articles:
        cat = category_map.get(art["url"], "Uncategorized")
        categories.setdefault(cat, []).append(art)

    # Sort articles within each category alphabetically by title
    for cat_articles in categories.values():
        cat_articles.sort(key=lambda a: a["title"].lower())

    # Sort categories alphabetically; Uncategorized always last
    sorted_cats = sorted(k for k in categories if k != "Uncategorized")
    if "Uncategorized" in categories:
        sorted_cats.append("Uncategorized")

    generated = datetime.date.today().isoformat()
    total_articles = len(articles)

    combined = _assemble_combined({k: categories[k] for k in sorted_cats}, generated, total_articles)

    word_count = len(combined.split())
    output_dir.mkdir(parents=True, exist_ok=True)

    if word_count > SPLIT_THRESHOLD:
        # Write per-category files — do NOT write portal_docs.md
        output_files = []
        for cat in sorted_cats:
            slug = slugify(cat)
            cat_content = _assemble_category_file(cat, categories[cat], generated)
            path = output_dir / f"portal_docs_{slug}.md"
            _write_atomic(path, cat_content)
            output_files.append(str(path))
    else:
        path = output_dir / "portal_docs.md"
        _write_atomic(path, combined)
        output_files = [str(path)]

    result = {
        "word_count": word_count,
        "output_files": output_files,
        "article_count": len(articles),
        "failed_count": failed_count,
    }

    return result


def force_reset(visited_path: pathlib.Path, cache_dir: pathlib.Path, frontier_path: pathlib.Path) -> None:
    """Reset visited state to {}, empty the cache directory, and delete frontier.json.

    Writes an empty dict to visited_path (atomic via .tmp), removes all
    files inside cache_dir then recreates the directory, and deletes
    frontier_path so the next crawl starts fresh from PORTAL_HOME.
    """
    import json
    import shutil

    # Atomically write empty visited state
    visited_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = visited_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump({}, fh)
    os.replace(tmp_path, visited_path)

    # Remove and recreate cache directory
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Delete frontier so the crawl restarts from PORTAL_HOME (INCR-02)
    frontier_path.unlink(missing_ok=True)
