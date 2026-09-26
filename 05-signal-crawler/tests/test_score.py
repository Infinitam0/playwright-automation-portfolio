"""Scorer tests — pure formula and DB-integration paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scanner import db as db_module
from scanner.extract.catalog import Catalog, CatalogEntry
from scanner.score.app_scorer import AppScorer, AppStats, compute_score

# ---------- pure compute_score ----------

NOW = datetime(2026, 5, 13, tzinfo=UTC)


def test_zero_mentions_scores_zero() -> None:
    assert compute_score(AppStats(mentions=0), now=NOW) == 0.0


def test_more_mentions_increases_score() -> None:
    a = compute_score(AppStats(mentions=1, sources=1, latest_iso=NOW.isoformat()), now=NOW)
    b = compute_score(AppStats(mentions=10, sources=1, latest_iso=NOW.isoformat()), now=NOW)
    assert b > a


def test_cross_source_breadth_boosts_score() -> None:
    base = AppStats(mentions=5, sources=1, latest_iso=NOW.isoformat())
    wide = AppStats(mentions=5, sources=3, latest_iso=NOW.isoformat())
    assert compute_score(wide, now=NOW) > compute_score(base, now=NOW)


def test_shutdown_vocab_weights_more_than_alternative() -> None:
    shut = AppStats(mentions=1, sources=1, shut=0.9, latest_iso=NOW.isoformat())
    alt = AppStats(mentions=1, sources=1, alt=0.9, latest_iso=NOW.isoformat())
    assert compute_score(shut, now=NOW) > compute_score(alt, now=NOW)


def test_alttto_badge_boost() -> None:
    plain = AppStats(mentions=1, sources=1, latest_iso=NOW.isoformat())
    badged = AppStats(mentions=1, sources=1, badge=True, latest_iso=NOW.isoformat())
    assert compute_score(badged, now=NOW) > compute_score(plain, now=NOW)


def test_recency_decay_reduces_old_signals() -> None:
    fresh = AppStats(mentions=5, sources=2, shut=0.9, latest_iso=NOW.isoformat())
    one_year = (NOW - timedelta(days=365)).isoformat()
    stale = AppStats(mentions=5, sources=2, shut=0.9, latest_iso=one_year)
    # Decay is exp(-365/365) ~= 0.368 → stale ~= 0.37 * fresh
    fresh_s = compute_score(fresh, now=NOW)
    stale_s = compute_score(stale, now=NOW)
    assert stale_s < fresh_s
    assert 0.30 * fresh_s < stale_s < 0.45 * fresh_s


def test_wunderlist_style_signal_scores_above_two() -> None:
    """A realistic well-attested discontinued app should clear the 2.0 cutoff."""
    s = AppStats(mentions=8, sources=2, shut=0.9, alt=0.6, badge=True, latest_iso=NOW.isoformat())
    assert compute_score(s, now=NOW) >= 2.0


# ---------- DB-backed AppScorer ----------


async def _seed(conn) -> None:
    # Sources -> inbox -> signals scaffolding for one well-attested app.
    await conn.execute("INSERT INTO sources(name, rate_per_min) VALUES ('hn', 60), ('alt', 30)")
    rows = [
        (
            "hn",
            "1",
            '{"title":"Wunderlist is shutting down"}',
            "h1",
            "shutdown",
            "Wunderlist is shutting down",
            0.90,
        ),
        (
            "hn",
            "2",
            '{"comment_text":"alternative to Wunderlist?"}',
            "h2",
            "alt_to",
            "alternative to Wunderlist",
            0.45,
        ),
        ("alt", "a1", "Wunderlist page", "ha1", "disc", "Wunderlist discontinued", 0.90),
    ]
    for src, sid, raw, h, stype, stext, conf in rows:
        cur = await conn.execute(
            "INSERT INTO inbox(source, source_item_id, raw_content, hash) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (src, sid, raw, h),
        )
        inbox_id = (await cur.fetchone())[0]
        await cur.close()
        await conn.execute(
            "INSERT INTO signals(inbox_id, signal_type, signal_text, mentioned_app, confidence) "
            "VALUES (?, ?, ?, 'Wunderlist', ?)",
            (inbox_id, stype, stext, conf),
        )
    await conn.commit()


@pytest.mark.asyncio
async def test_app_scorer_writes_apps_row(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        await _seed(conn)
        catalog = Catalog([CatalogEntry(name="Wunderlist", discontinued=True)])
        scorer = AppScorer(catalog=catalog)
        score = await scorer.update(conn, "Wunderlist")
        assert score > 0.0

        cur = await conn.execute(
            "SELECT score, mention_count, source_count, alttto_discontinued "
            "FROM apps WHERE name='Wunderlist'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] > 0.0
        assert row[1] == 3  # 3 distinct inbox rows
        assert row[2] == 2  # hn + alt
        assert row[3] == 1  # discontinued badge from catalog
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_update_all_mentioned_iterates_signals(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        await _seed(conn)
        await conn.execute(
            "INSERT INTO inbox(source, source_item_id, raw_content, hash) "
            "VALUES ('hn', '99', 'Periscope is dead', 'h99')"
        )
        cur = await conn.execute("SELECT id FROM inbox WHERE source_item_id='99'")
        iid = (await cur.fetchone())[0]
        await cur.close()
        await conn.execute(
            "INSERT INTO signals(inbox_id, signal_type, signal_text, mentioned_app, confidence) "
            "VALUES (?, 'dead', 'Periscope is dead', 'Periscope', 0.80)",
            (iid,),
        )
        await conn.commit()

        scorer = AppScorer(catalog=Catalog([]))
        n = await scorer.update_all_mentioned(conn)
        assert n == 2  # Wunderlist + Periscope

        cur = await conn.execute("SELECT COUNT(*) FROM apps")
        assert (await cur.fetchone())[0] == 2
    finally:
        await conn.close()
