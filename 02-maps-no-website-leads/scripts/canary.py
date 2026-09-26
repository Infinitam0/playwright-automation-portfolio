"""Run this before every scrape session.

Google rotates the payload layout and the record offsets silently. When that
happens the scraper does not crash — it returns `website: None` for everything
and produces a beautiful spreadsheet of businesses that all have websites. That
failure is invisible unless something checks it, which is what this is.

Each entry is a place whose web presence is known and stable. A mismatch means
STOP: fix selectors.py before trusting any output.

    python scripts/canary.py

Exit 0 = safe to scrape. Non-zero = do not trust results until fixed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.classify import classify  # noqa: E402
from src.logging_conf import setup as setup_logging  # noqa: E402
from src.models import WebsiteTier  # noqa: E402
from src.scraper.browser import create_browser_session  # noqa: E402
from src.scraper.detail import fetch_detail  # noqa: E402
from src.scraper.timing import jittered_delay  # noqa: E402

setup_logging(logging.WARNING)

# (search url, expected tier, a substring the website must contain or None)
#
# Large chains are used deliberately: their listings are actively managed, so
# "has a website" is not going to quietly change and give a false alarm.
CANARIES = [
    (
        "https://www.google.com/maps/search/starbucks+reserve+roastery+seattle",
        WebsiteTier.REAL,
        "starbucksreserve.com",
    ),
    (
        "https://www.google.com/maps/search/apple+store+fifth+avenue+new+york",
        WebsiteTier.REAL,
        "apple.com",
    ),
    (
        "https://www.google.com/maps/search/rijksmuseum+amsterdam",
        WebsiteTier.REAL,
        "rijksmuseum.nl",
    ),
    (
        "https://www.google.com/maps/search/eiffel+tower+paris",
        WebsiteTier.REAL,
        None,
    ),
]

# If more than this fraction of canaries lose their website, the extraction is
# broken rather than the businesses having changed. One drifting listing is
# noise; most of them dropping at once is a layout change.
FAIL_THRESHOLD = 0.25

# Fields every real listing has. Phone, rating, category and plus code are
# legitimately absent for plenty of places (landmarks have no phone), so a miss
# there is not evidence of rot — flagging it would just teach us to ignore the
# alarm, which defeats the point of having one.
CORE_FIELDS = {"name", "address", "place_id", "fid"}


async def main() -> int:
    results: list[tuple[str, bool, str]] = []

    async with create_browser_session(ROOT / "data" / "profile") as context:
        page = await context.new_page()
        for url, expected_tier, must_contain in CANARIES:
            label = url.rsplit("/", 1)[-1]
            try:
                detail = await fetch_detail(page, url)
            except Exception as e:  # noqa: BLE001
                results.append((label, False, f"fetch failed: {type(e).__name__}: {e}"))
                continue

            tier = classify(detail.website)
            problems = []
            if tier is not expected_tier:
                problems.append(f"tier {tier} != expected {expected_tier}")
            if must_contain and must_contain not in (detail.website or ""):
                problems.append(f"website {detail.website!r} lacks {must_contain!r}")
            if not detail.name:
                problems.append("no name extracted")
            core_misses = CORE_FIELDS.intersection(detail.extraction_misses)
            if core_misses:
                problems.append(f"core fields missing: {sorted(core_misses)}")

            note = f"ok ({detail.website})"
            if optional := set(detail.extraction_misses) - CORE_FIELDS:
                note += f"  [optional absent: {sorted(optional)}]"

            results.append((label, not problems, "; ".join(problems) or note))
            await jittered_delay(3.0, 0.5)

    print("\n" + "=" * 72)
    for label, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         {msg}")
    print("=" * 72)

    failed = sum(1 for _, ok, _ in results if not ok)
    ratio = failed / len(results) if results else 1.0

    if ratio > FAIL_THRESHOLD:
        print(
            f"\n{failed}/{len(results)} canaries failed — STOP.\n"
            "Google has most likely moved the payload layout or the record\n"
            "offsets. Re-derive them from a live payload, then fix\n"
            "src/scraper/selectors.py. Do not trust scrape output until this passes."
        )
        return 1

    if failed:
        print(f"\n{failed}/{len(results)} failed but under threshold — check the detail above.")
    else:
        print(f"\nAll {len(results)} canaries passed. Safe to scrape.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
