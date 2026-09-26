"""Reddit scraper tests — parse old.reddit.com search HTML."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

from scanner.scrapers.reddit import RedditScraper

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"


def _snapshot() -> str:
    return (SNAPSHOTS / "reddit_search.html").read_text(encoding="utf-8")


# ---- parse_results ----


def test_parse_results_extracts_three_posts() -> None:
    results = RedditScraper.parse_results(_snapshot())
    assert len(results) == 3
    ids = [r["id"] for r in results]
    assert ids == ["t3_aaaa1111", "t3_bbbb2222", "t3_cccc3333"]


def test_parse_results_captures_title_and_selftext() -> None:
    results = RedditScraper.parse_results(_snapshot())
    r0 = results[0]
    assert "Wunderlist" in r0["title"]
    assert "TickTick" in r0["selftext"]


def test_parse_results_captures_subreddit() -> None:
    results = RedditScraper.parse_results(_snapshot())
    subs = {r["subreddit"] for r in results}
    assert subs == {"androidapps", "iosgaming", "AskReddit"}


def test_parse_results_captures_permalink() -> None:
    results = RedditScraper.parse_results(_snapshot())
    for r in results:
        assert r["permalink"].startswith("https://www.reddit.com/r/")


def test_parse_results_parses_iso_timestamps() -> None:
    results = RedditScraper.parse_results(_snapshot())
    for r in results:
        assert r["created_utc"] > 1700_000_000  # post-2023


def test_parse_results_empty_on_garbage_html() -> None:
    assert RedditScraper.parse_results("<html><body>nothing</body></html>") == []


# ---- parse_next_after ----


def test_parse_next_after_returns_token() -> None:
    assert RedditScraper.parse_next_after(_snapshot()) == "t3_cccc3333"


def test_parse_next_after_none_when_no_next_link() -> None:
    assert RedditScraper.parse_next_after("<html></html>") is None


# ---- cursor ----


def test_parse_since_handles_garbage() -> None:
    assert RedditScraper._parse_since(None) == 0
    assert RedditScraper._parse_since("") == 0
    assert RedditScraper._parse_since("garbage") == 0
    assert RedditScraper._parse_since("1700000000") == 1700000000


# ---- time window picker ----


def test_time_window_zero_is_month() -> None:
    assert RedditScraper._time_window_for(0) == "month"


def test_time_window_picks_smallest_containing_window() -> None:
    """Recent cursor -> small window. Old cursor -> larger window."""
    from datetime import datetime

    now_ts = int(datetime.now(UTC).timestamp())
    assert RedditScraper._time_window_for(now_ts - 30) == "hour"
    assert RedditScraper._time_window_for(now_ts - 3_600 * 5) == "day"
    assert RedditScraper._time_window_for(now_ts - 86_400 * 3) == "week"
    assert RedditScraper._time_window_for(now_ts - 86_400 * 15) == "month"


# ---- text() classmethod (used by extractor) ----


def test_text_concatenates_title_selftext_subreddit() -> None:
    raw = json.dumps(
        {
            "title": "Looking for alternative to Wunderlist",
            "selftext": "MS killed it",
            "subreddit": "androidapps",
        }
    )
    text = RedditScraper.text(raw)
    assert "Wunderlist" in text
    assert "MS killed it" in text
    assert "androidapps" in text


def test_text_falls_back_on_bad_json() -> None:
    assert RedditScraper.text("not json at all") == "not json at all"
