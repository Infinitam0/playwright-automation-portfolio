"""Pattern + catalog + extractor tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanner import db as db_module
from scanner.extract.catalog import Catalog, CatalogEntry, disambiguate
from scanner.extract.extractor import Extractor
from scanner.extract.patterns import find_all
from scanner.scrapers.hackernews import HackernewsScraper

# ---------- patterns ----------


def test_pattern_matches_alternative_to_titlecased() -> None:
    hits = find_all("What's a good alternative to Wunderlist now?")
    assert any(stype == "alt_to" and "Wunderlist" in cand for stype, _, cand, _ in hits)


def test_pattern_skips_lowercase_app_in_low_confidence_match() -> None:
    """`alternative to inbox` should not bind 'inbox' as a TitleCased app."""
    hits = find_all("Is there an alternative to inbox?")
    # Our APP regex requires (?-i:[A-Z]) for the unquoted form, so 'inbox'
    # never becomes a candidate. The pattern simply fails to match here.
    assert all(cand.lower().strip("\"'") != "inbox" for _, _, cand, _ in hits)


def test_pattern_matches_quoted_app_name() -> None:
    hits = find_all('looking for an alternative to "Sunrise Calendar" since shutdown')
    assert any("Sunrise Calendar" in cand for _, _, cand, _ in hits)


def test_pattern_matches_shutdown_high_confidence() -> None:
    hits = find_all("Wunderlist is shutting down in May.")
    types = {stype for stype, _, _, _ in hits}
    assert "shutdown" in types
    # The shutdown match carries 0.90 base confidence.
    confs = [c for s, _, _, c in hits if s == "shutdown"]
    assert max(confs) >= 0.80


def test_pattern_matches_rip() -> None:
    hits = find_all("RIP Vine")
    assert any(stype == "rip" and "Vine" in cand for stype, _, cand, _ in hits)


def test_pattern_matches_eol_case_sensitive() -> None:
    """EOL is an acronym — only uppercase counts."""
    hits = find_all("Foo EOL announced today.")
    assert any(stype == "eol" for stype, _, _, _ in hits)
    hits = find_all("Foo eol announced today.")
    assert not any(stype == "eol" for stype, _, _, _ in hits)


def test_pattern_matches_discontinued() -> None:
    hits_a = find_all("Mailbox is discontinued.")
    hits_b = find_all("Mailbox discontinued.")
    assert any(s == "disc" for s, _, _, _ in hits_a)
    assert any(s == "disc" for s, _, _, _ in hits_b)


# ---------- catalog ----------


def test_catalog_loads_from_yaml() -> None:
    cat = Catalog.load(Path(__file__).resolve().parent.parent / "known_apps.yaml")
    assert "Wunderlist" in cat
    assert "Periscope" in cat
    assert cat.is_discontinued("Wunderlist")
    # Sunlit is seeded as `status: active`.
    assert not cat.is_discontinued("Sunlit")


def test_catalog_resolve_exact_alias() -> None:
    cat = Catalog(
        [
            CatalogEntry(name="Apollo for Reddit", aliases=["apollo", "apollo app"]),
        ]
    )
    assert cat.resolve("Apollo") == "Apollo for Reddit"
    assert cat.resolve("apollo app") == "Apollo for Reddit"


def test_catalog_resolve_fuzzy() -> None:
    cat = Catalog([CatalogEntry(name="Wunderlist")])
    # Single-char typo (wunderlist -> wunderlst, ratio ~94)
    assert cat.resolve("Wunderlst") == "Wunderlist"
    # Far enough that fuzzy fails
    assert cat.resolve("Notion") is None


def test_catalog_resolve_returns_none_when_empty() -> None:
    cat = Catalog([])
    assert cat.resolve("Anything") is None


def test_catalog_load_missing_file_returns_empty() -> None:
    cat = Catalog.load("does-not-exist.yaml")
    assert len(cat) == 0


# ---------- disambiguation ----------


def test_disambiguate_high_confidence_accepts_unknown() -> None:
    """A high-conf shutdown match for a not-yet-cataloged app is kept."""
    cat = Catalog([])
    assert disambiguate("ObscureApp", 0.90, cat) == "ObscureApp"


def test_disambiguate_low_confidence_rejects_uncatalogued() -> None:
    cat = Catalog([])
    assert disambiguate("ObscureApp", 0.45, cat) is None


def test_disambiguate_low_confidence_accepts_catalog_hit() -> None:
    cat = Catalog([CatalogEntry(name="Wunderlist")])
    assert disambiguate("Wunderlist", 0.45, cat) == "Wunderlist"


def test_disambiguate_blocks_stop_words_under_low_confidence() -> None:
    cat = Catalog([CatalogEntry(name="Inbox")])
    assert disambiguate("inbox", 0.45, cat) is None
    # High confidence still keeps the catalog match.
    assert disambiguate("inbox", 0.90, cat) == "Inbox"


# ---------- end-to-end extractor against SQLite ----------


@pytest.mark.asyncio
async def test_extractor_processes_inbox_row(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        # Stuff one HN-style row in.
        text = (
            "What's a good alternative to Wunderlist now that Microsoft "
            "killed it? Mailbox is also dead at this point. RIP Vine."
        )
        await conn.execute(
            "INSERT INTO inbox(source, source_item_id, raw_content, hash) "
            "VALUES ('hackernews', 'hn_test_1', ?, 'h1')",
            ('{"title":"alternative to Wunderlist","comment_text":"' + text + '"}',),
        )
        await conn.commit()

        scrapers = {"hackernews": HackernewsScraper}
        cat = Catalog.load(Path(__file__).resolve().parent.parent / "known_apps.yaml")
        extractor = Extractor(scrapers=scrapers, catalog=cat)
        signals = await extractor.process_one(conn, 1)
        assert signals >= 3  # alt_to/Wunderlist, dead/Mailbox, rip/Vine at minimum

        rows = await (
            await conn.execute(
                "SELECT signal_type, mentioned_app FROM signals ORDER BY mentioned_app"
            )
        ).fetchall()
        apps = {r[1] for r in rows}
        types = {r[0] for r in rows}
        assert "Wunderlist" in apps
        assert "Mailbox" in apps
        assert "Vine" in apps
        # Each app reaches signals via at least one matching pattern.
        assert {"alt_to", "dead", "rip"} & types

        # extracted flag flipped.
        cur = await conn.execute("SELECT extracted FROM inbox WHERE id=1")
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_rebuild_is_deterministic(tmp_db: Path, migrations_dir: Path) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    conn = await db_module.connect(tmp_db)
    try:
        await conn.execute(
            "INSERT INTO inbox(source, source_item_id, raw_content, hash) "
            "VALUES ('hackernews', 'x', "
            "  '{\"title\":\"Wunderlist is shutting down\"}', 'h')"
        )
        await conn.commit()

        scrapers = {"hackernews": HackernewsScraper}
        cat = Catalog.load(Path(__file__).resolve().parent.parent / "known_apps.yaml")
        ext = Extractor(scrapers=scrapers, catalog=cat)

        rows1, sigs1 = await ext.reset_and_reprocess(conn)
        rows2, sigs2 = await ext.reset_and_reprocess(conn)
        assert (rows1, sigs1) == (rows2, sigs2)
        assert sigs1 >= 1
    finally:
        await conn.close()
