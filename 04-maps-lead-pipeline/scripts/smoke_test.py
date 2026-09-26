"""Offline end-to-end proof. No network, no API keys, no browser.

Feeds fixture companies + fixture website HTML through the real pipeline stages
(dedup -> enrich -> score -> draft(template) -> lint -> export) and asserts a
company flows all the way to a clean CSV row. Run: `python scripts/smoke_test.py`.
All companies and addresses below are fictional; domains are RFC 2606 reserved.
"""

from __future__ import annotations

import asyncio
import csv
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db  # noqa: E402
from src.config import Settings  # noqa: E402
from src.enrich.website import enrich_company  # noqa: E402
from src.loaders import load_exclusions, load_verticals  # noqa: E402
from src.models import RawCandidate, Source  # noqa: E402
from src.outreach.draft import TemplateDrafter, build_fields  # noqa: E402
from src.outreach.lint import lint_draft  # noqa: E402
from src.pipeline import run_export  # noqa: E402
from src.score import score_company  # noqa: E402

SITE = "https://www.example.org"
FIXTURES = {
    SITE: """<html><body><h1>Roofing contractor</h1>
        <p>Roof repairs and new roofing. Free quote.</p>
        <a href="/contact">Contact</a></body></html>""",
    f"{SITE}/contact": """<html><body>
        <p>Mail: <a href="mailto:info@example.org">info@example.org</a>
        or jane.doe [at] example [dot] org</p>
        <p>Phone: 020-0000000. ISO 9001 certified.</p></body></html>""",
}


async def fake_fetch(url: str) -> str:
    if url in FIXTURES:
        return FIXTURES[url]
    raise RuntimeError(f"404 not found: {url}")


async def main() -> None:
    settings = Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        sender_name="Alex Example",
        sender_company="Example Co",
        sender_email="hello@example.com",
    )
    verticals_cfg = load_verticals(settings.config_dir)
    exclusions = load_exclusions(settings.config_dir)

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.sqlite")

        # The same company arriving twice (e.g. from two different queries), once
        # with a website and once with only an internationally formatted phone.
        # Dedup must fold them into one row.
        places = RawCandidate(
            name="Demo Roofing Co", website_url=SITE, phone="020-0000000",
            street="Examplestreet 1, Springfield", postcode="1234 AB",
            city="Springfield", province="region-a", country="NL",
            google_rating=4.8, google_review_count=40, google_place_id="p1",
            source=Source(name="places", source_id="p1"),
        )
        second = RawCandidate(
            name="Demo Roofing Co", phone="+31 20 000 0000", postcode="1234AB",
            city="Springfield", source=Source(name="fixture", source_id="f1"),
        )
        db.upsert_company(conn, places.to_company(vertical_hint="roofing"))
        db.upsert_company(conn, second.to_company())
        rows = db.iter_companies(conn)
        assert len(rows) == 1, f"dedup failed: {len(rows)} rows"
        c = rows[0]
        assert c.source_names == "fixture|places", c.source_names

        await enrich_company(
            c, fetch=fake_fetch, verticals_cfg=verticals_cfg, settings=settings,
            negative_keywords=exclusions["out_of_scope_keywords"],
            segment_signals=exclusions["segment_signals"],
        )
        assert c.enrichment_status == "enriched"
        assert c.best_email() == "info@example.org", c.emails
        assert "jane.doe@example.org" in {e.address for e in c.emails}
        assert "ISO 9001" in c.certs and "roofing" in c.verticals_served

        score_company(
            c, exclusions=exclusions, suppression={"emails": set(), "domains": set()},
            needed_verticals=["roofing"], province_tier=1, settings=settings,
        )
        assert c.priority_tier == "A", (c.fit_score, c.score_reasons)

        fields = build_fields(c, verticals_cfg, settings)
        c.draft_subject, c.draft_body = await TemplateDrafter().draft_one(c, fields)
        assert lint_draft(c.draft_subject, c.draft_body) == [], "template draft not lint-clean"
        c.draft_status = "drafted"
        db.save_company(conn, c)

        # An unconfigured sender must be caught by the lint, never exported.
        bare = build_fields(c, verticals_cfg, Settings(_env_file=None))
        assert lint_draft(*await TemplateDrafter().draft_one(c, bare))

        out = Path(tmp) / "leads.csv"
        assert run_export(conn, settings, out=out) == 1
        with out.open(encoding="utf-8-sig", newline="") as fh:
            row = next(csv.DictReader(fh))
        assert row["contact_email"] == "info@example.org"
        assert row["phone"].startswith("0"), row["phone"]
        conn.close()
    print("smoke test OK")


if __name__ == "__main__":
    asyncio.run(main())
