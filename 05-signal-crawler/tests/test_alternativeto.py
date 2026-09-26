"""AlternativeTo scraper tests — pure parsers against saved snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanner.scrapers.alternativeto import AlternativeToScraper

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"


# ---- is_discontinued ----


def test_is_discontinued_true_on_wunderlist_snapshot() -> None:
    html = (SNAPSHOTS / "alternativeto_page.html").read_text(encoding="utf-8")
    assert AlternativeToScraper.is_discontinued(html) is True


def test_is_discontinued_false_on_active_snapshot() -> None:
    html = (SNAPSHOTS / "alternativeto_active.html").read_text(encoding="utf-8")
    assert AlternativeToScraper.is_discontinued(html) is False


def test_is_discontinued_robust_to_no_alerts_section() -> None:
    """A page that says 'discontinued alternatives' incidentally must not flag."""
    html = "<html><body>Some discontinued alternatives are not always best.</body></html>"
    assert AlternativeToScraper.is_discontinued(html) is False


def test_is_discontinued_picks_up_shutdown_phrasing() -> None:
    html = "<html><body>This app shut down on March 15, 2024.</body></html>"
    assert AlternativeToScraper.is_discontinued(html) is True


# ---- _slugify ----


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Wunderlist", "wunderlist"),
        ("Apollo for Reddit", "apollo-for-reddit"),
        ("Google Inbox", "google-inbox"),
        ("Sunrise Calendar", "sunrise-calendar"),
        ("X.com (Twitter)", "xcom-twitter"),
        ("  Mailbox   ", "mailbox"),
        ("Multi--Hyphen", "multi-hyphen"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert AlternativeToScraper._slugify(name) == expected


# ---- cursor parsing ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 0),
        ("", 0),
        ("0", 0),
        ("7", 7),
        ("not-an-int", 0),
        ("-5", 0),  # negatives floor to 0
    ],
)
def test_parse_cursor(raw: str | None, expected: int) -> None:
    assert AlternativeToScraper._parse_cursor(raw) == expected


# ---- candidate loading ----


def test_candidates_from_catalog_pulls_known_apps() -> None:
    scraper = AlternativeToScraper(
        catalog_path=Path(__file__).resolve().parent.parent / "known_apps.yaml"
    )
    cands = scraper._candidates_from_catalog()
    assert "Wunderlist" in cands
    assert "Periscope" in cands
    # Sorted for deterministic cursor semantics.
    assert cands == sorted(cands)
