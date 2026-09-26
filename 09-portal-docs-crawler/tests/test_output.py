"""Unit tests for output assembly module (Phase 4).

Covers: OUT-01, OUT-02, OUT-03, OUT-04, INCR-02, DX-02
"""

import pathlib
from unittest.mock import MagicMock

import main
import output
import scraper

# ---------------------------------------------------------------------------
# Helpers to build fake cache files matching Phase 3 format
# ---------------------------------------------------------------------------


def _make_article(title: str, url: str, body: str = "Some content here.") -> str:
    return f"# {title}\n**Source:** {url}\n**Scraped:** 2000-01-03\n\n{body}\n"


def _make_failed_article(title: str, url: str) -> str:
    return f"# {title}\n**Source:** {url}\n**Scraped:** 2000-01-03\n\n<!-- extraction failed: test -->\n"


def _write_cache_article(cache_dir: pathlib.Path, filename: str, content: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# OUT-01 — single combined file
# ---------------------------------------------------------------------------


async def test_assemble_writes_single_file(tmp_path, monkeypatch):
    """assemble() writes output/portal_docs.md containing all article titles."""
    tmp_cache = tmp_path / "cache"
    tmp_output = tmp_path / "output"

    _write_cache_article(
        tmp_cache, "article1.md", _make_article("Getting Started", "https://support.example.com/articles/1")
    )
    _write_cache_article(
        tmp_cache,
        "article2.md",
        _make_article("Advanced Configuration", "https://support.example.com/articles/2"),
    )

    page = MagicMock()
    context = MagicMock()

    await output.assemble(tmp_cache, tmp_output, page, context, "", "")

    combined = tmp_output / "portal_docs.md"
    assert combined.exists(), "portal_docs.md should be written"
    content = combined.read_text(encoding="utf-8")
    assert "Getting Started" in content
    assert "Advanced Configuration" in content


# ---------------------------------------------------------------------------
# OUT-02 — grouped by category
# ---------------------------------------------------------------------------


async def test_articles_grouped_by_category(tmp_path, monkeypatch):
    """assemble() groups articles under H1 category headings."""
    tmp_cache = tmp_path / "cache"
    tmp_output = tmp_path / "output"

    _write_cache_article(
        tmp_cache, "article1.md", _make_article("Article Alpha", "https://support.example.com/articles/1")
    )
    _write_cache_article(
        tmp_cache, "article2.md", _make_article("Article Beta", "https://support.example.com/articles/2")
    )

    page = MagicMock()
    context = MagicMock()

    await output.assemble(tmp_cache, tmp_output, page, context, "", "")

    combined = tmp_output / "portal_docs.md"
    assert combined.exists()
    content = combined.read_text(encoding="utf-8")
    # At least one H1 section heading for a category must be present
    assert "# " in content, "Expected H1 category headings in output"


# ---------------------------------------------------------------------------
# OUT-03 — table of contents
# ---------------------------------------------------------------------------


async def test_toc_in_output(tmp_path, monkeypatch):
    """assemble() produces document starting with title header and TOC with anchors."""
    tmp_cache = tmp_path / "cache"
    tmp_output = tmp_path / "output"

    _write_cache_article(
        tmp_cache, "article1.md", _make_article("Article One", "https://support.example.com/articles/1")
    )
    _write_cache_article(
        tmp_cache, "article2.md", _make_article("Article Two", "https://support.example.com/articles/2")
    )

    page = MagicMock()
    context = MagicMock()

    await output.assemble(tmp_cache, tmp_output, page, context, "", "")

    combined = tmp_output / "portal_docs.md"
    assert combined.exists()
    content = combined.read_text(encoding="utf-8")
    assert content.startswith("# Support Portal Documentation"), (
        "Output must start with '# Support Portal Documentation' header"
    )
    # TOC anchor link format: [Category](#category-slug)
    assert "](#" in content, "Expected TOC anchor links in output"


# ---------------------------------------------------------------------------
# OUT-04 — split when over word threshold
# ---------------------------------------------------------------------------


async def test_split_when_over_threshold(tmp_path, monkeypatch):
    """assemble() writes per-category files when combined word count exceeds 350,000."""
    tmp_cache = tmp_path / "cache"
    tmp_output = tmp_path / "output"

    # One large article body guaranteeing >350,000 words
    large_body = "word " * 360_000
    _write_cache_article(
        tmp_cache,
        "large_article.md",
        _make_article("Huge Article", "https://support.example.com/articles/99", body=large_body),
    )

    page = MagicMock()
    context = MagicMock()

    await output.assemble(tmp_cache, tmp_output, page, context, "", "")

    # At least one per-category split file must exist
    split_files = list(tmp_output.glob("portal_docs_*.md"))
    assert len(split_files) > 0, (
        f"Expected per-category portal_docs_{{slug}}.md files when word count exceeds 350k, "
        f"found: {list(tmp_output.iterdir())}"
    )


# ---------------------------------------------------------------------------
# OUT-04 — no combined file on split
# ---------------------------------------------------------------------------


async def test_no_combined_file_on_split(tmp_path, monkeypatch):
    """assemble() does NOT write portal_docs.md when splitting into per-category files."""
    tmp_cache = tmp_path / "cache"
    tmp_output = tmp_path / "output"

    large_body = "word " * 360_000
    _write_cache_article(
        tmp_cache,
        "large_article.md",
        _make_article("Huge Article", "https://support.example.com/articles/99", body=large_body),
    )

    page = MagicMock()
    context = MagicMock()

    await output.assemble(tmp_cache, tmp_output, page, context, "", "")

    combined = tmp_output / "portal_docs.md"
    assert not combined.exists(), "portal_docs.md must NOT exist when output is split into per-category files"


# ---------------------------------------------------------------------------
# INCR-02 — force reset
# ---------------------------------------------------------------------------


def test_force_reset(tmp_path, monkeypatch):
    """force_reset() clears visited.json to {}, empties the cache directory, and deletes frontier.json."""
    import json

    # Set up tmp paths
    tmp_state = tmp_path / "state"
    tmp_state.mkdir()
    visited_path = tmp_state / "visited.json"
    visited_path.write_text(json.dumps({"https://example.com": {}}), encoding="utf-8")

    tmp_cache = tmp_path / "cache"
    tmp_cache.mkdir()
    (tmp_cache / "somefile.md").write_text("content", encoding="utf-8")

    # Set up frontier.json to simulate an interrupted crawl
    tmp_frontier_path = tmp_state / "frontier.json"
    tmp_frontier_path.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(scraper, "VISITED_PATH", visited_path)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_cache)

    output.force_reset(visited_path, tmp_cache, tmp_frontier_path)

    # visited.json should be {}
    remaining = json.loads(visited_path.read_text(encoding="utf-8"))
    assert remaining == {}, f"Expected empty visited state, got: {remaining}"

    # cache dir should be empty
    cache_files = list(tmp_cache.iterdir())
    assert cache_files == [], f"Expected empty cache dir, found: {cache_files}"

    # frontier.json should be deleted
    assert not tmp_frontier_path.exists(), "Expected frontier.json to be deleted by force_reset"


# ---------------------------------------------------------------------------
# DX-02 — run summary
# ---------------------------------------------------------------------------


def test_run_summary(capsys):
    """main._print_run_summary() prints structured run summary block."""
    crawl_stats = {"scraped": 5, "skipped": 10, "failed": 1}
    extraction_stats = {"failed": 2}
    assembly_stats = {"word_count": 5000, "output_files": ["output/portal_docs.md"], "failed_count": 0}

    main._print_run_summary(crawl_stats, extraction_stats, assembly_stats)

    captured = capsys.readouterr()
    out = captured.out
    assert "Articles scraped" in out, f"Expected 'Articles scraped' in summary, got:\n{out}"
    assert "Word count" in out, f"Expected 'Word count' in summary, got:\n{out}"
    assert "Output" in out, f"Expected 'Output' in summary, got:\n{out}"


# ---------------------------------------------------------------------------
# DX-02 — assembly progress format
# ---------------------------------------------------------------------------


async def test_assembly_progress_format(tmp_path, monkeypatch, capsys):
    """assemble() prints [N/M] Writing: {title} for each article processed."""
    tmp_cache = tmp_path / "cache"
    tmp_output = tmp_path / "output"

    titles = ["First Article", "Second Article", "Third Article"]
    for i, title in enumerate(titles, start=1):
        _write_cache_article(
            tmp_cache, f"article{i}.md", _make_article(title, f"https://support.example.com/articles/{i}")
        )

    page = MagicMock()
    context = MagicMock()

    await output.assemble(tmp_cache, tmp_output, page, context, "", "")

    captured = capsys.readouterr()
    out = captured.out

    # All three [N/3] Writing: lines must appear
    assert "[1/3] Writing:" in out, f"Expected '[1/3] Writing:' in output, got:\n{out}"
    assert "[2/3] Writing:" in out, f"Expected '[2/3] Writing:' in output, got:\n{out}"
    assert "[3/3] Writing:" in out, f"Expected '[3/3] Writing:' in output, got:\n{out}"


# ---------------------------------------------------------------------------
# Phase 6 — Nyquist coverage: slugify and heading_anchor
# ---------------------------------------------------------------------------


def test_slugify_accents():
    assert output.slugify("Café Menü") == "cafe-menu"
    assert output.slugify("Billing & Admin") == "billing-admin"


def test_heading_anchor():
    assert output.heading_anchor("Getting Started") == "getting-started"
    assert output.heading_anchor("API: Configuration") == "api-configuration"
