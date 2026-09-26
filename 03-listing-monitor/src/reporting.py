"""Per-area speed-to-sell summary for the Looker dashboard.

Reads the main listings tab, aggregates per area (median/mean days-on-market for
sold listings, counts, % sold), and writes a precomputed `summary` tab — because
Looker Studio cannot compute MEDIAN per group. Also ensures a
`price_delta_reference` tab exists (user-maintained asking-vs-sold price delta
per area, in %) and joins it into the summary.

`compute_summary` is a pure function (no IO) so it is unit-testable; the
`generate_report` entry point does the Google Sheets IO and is safe to call
best-effort from the scraper.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime
from typing import Any

from src.config import Settings
from src.models import SHEET_HEADERS
from src.scraper import site_profile as P
from src.storage.sheets import get_client, get_or_create_worksheet, get_worksheet

logger = logging.getLogger(__name__)

SUMMARY_TAB = "summary"
PRICE_DELTA_TAB = "price_delta_reference"

SUMMARY_HEADER = [
    "area", "count_total", "count_active", "count_sold", "pct_sold",
    "median_days_sold", "mean_days_sold", "price_delta_pct", "updated_at",
]
PRICE_DELTA_HEADER = ["area", "price_delta_pct", "source", "as_of"]
# Seed row for the user-maintained reference tab (asking vs sold price delta, %).
PRICE_DELTA_SEED = [
    ["example-city", "", "your data source — fill in", ""],
]

# Postal-code prefix (site_profile.POSTAL_REGION_PREFIX_LEN chars) -> area slug,
# for the price_delta fallback. Fill in for your own region, e.g. {"12": "example-city"}.
POSTAL_PREFIX_TO_AREA: dict[str, str] = {}


def _city_guess(postal_code: str) -> str:
    """Map a postal code to a coarse city for price_delta fallback."""
    pc = postal_code.strip()[: P.POSTAL_REGION_PREFIX_LEN]
    return POSTAL_PREFIX_TO_AREA.get(pc, "")


def compute_summary(
    records: list[dict],
    price_delta_lookup: dict[str, str],
    now_iso: str,
) -> list[list[str]]:
    """Aggregate per-area metrics. Pure function (no IO) for testability.

    Each record: {area, postal, status_current, closed(bool), sold(bool),
    days(int|None)}. Returns rows (without header) sorted by area.
    """
    by_area: dict[str, list[dict]] = {}
    for r in records:
        by_area.setdefault(r["area"] or "Unknown", []).append(r)

    rows: list[list[str]] = []
    for area in sorted(by_area):
        items = by_area[area]
        total = len(items)
        active = sum(1 for r in items if not r["closed"])
        sold_items = [r for r in items if r["sold"]]
        sold_count = len(sold_items)
        sold_days = [r["days"] for r in sold_items if r["days"] is not None]
        pct_sold = round(100 * sold_count / total, 1) if total else 0.0
        median_days = str(round(statistics.median(sold_days))) if sold_days else ""
        mean_days = f"{statistics.mean(sold_days):.1f}" if sold_days else ""

        # Price delta: exact area match, else city guessed from a sample postal code.
        price_delta = price_delta_lookup.get(area.lower(), "")
        if not price_delta:
            sample_pc = next((r["postal"] for r in items if r["postal"]), "")
            price_delta = price_delta_lookup.get(_city_guess(sample_pc), "")

        rows.append([
            area, str(total), str(active), str(sold_count), str(pct_sold),
            median_days, mean_days, price_delta, now_iso,
        ])
    return rows


def _col(header: list[str], name: str) -> int:
    """Index of a column by name, falling back to the canonical schema."""
    if name in header:
        return header.index(name)
    return SHEET_HEADERS.index(name)


def _load_price_delta(client, settings) -> dict[str, str]:
    """Get/create the price_delta_reference tab; seed if empty. Return the lookup."""
    ws = get_or_create_worksheet(client, settings, PRICE_DELTA_TAB, rows=200, cols=8)
    values = ws.get_all_values()
    if len(values) <= 1:  # newly created or header-only -> seed
        ws.update(range_name="A1", values=[PRICE_DELTA_HEADER] + PRICE_DELTA_SEED)
        values = [PRICE_DELTA_HEADER] + PRICE_DELTA_SEED
    lookup: dict[str, str] = {}
    for row in values[1:]:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            lookup[row[0].strip().lower()] = row[1].strip()
    return lookup


def generate_report(settings: Settings | None = None, client: Any = None) -> None:
    """Build the summary tab from the listings tab. Best-effort; logs and returns."""
    try:
        settings = settings or Settings()
        client = client or get_client(settings)

        values = get_worksheet(client, settings).get_all_values()
        if len(values) <= 1:
            logger.info("No listing rows — skipping summary report")
            return

        header = values[0]
        idx = {n: _col(header, n) for n in
               ("neighbourhood", "postal_code", "status_current",
                "closed_seen_at", "sold_flag", "days_on_market")}

        def cell(row, i):
            return row[i].strip() if len(row) > i and row[i] else ""

        records = []
        for row in values[1:]:
            if not cell(row, 0):  # no listing_id
                continue
            days_raw = cell(row, idx["days_on_market"])
            postal = cell(row, idx["postal_code"])
            # Neighbourhood is rarely captured, so group by postal district prefix
            # (always present) — prefer neighbourhood if it ever gets populated.
            area = cell(row, idx["neighbourhood"]) or postal[: P.POSTAL_DISTRICT_PREFIX_LEN]
            records.append({
                "area": area,
                "postal": postal,
                "status_current": cell(row, idx["status_current"]),
                "closed": bool(cell(row, idx["closed_seen_at"])),
                "sold": cell(row, idx["sold_flag"]).upper() == "TRUE",
                "days": int(days_raw) if days_raw.isdigit() else None,
            })

        price_delta_lookup = _load_price_delta(client, settings)
        now_iso = datetime.now().isoformat(timespec="seconds")
        rows = compute_summary(records, price_delta_lookup, now_iso)

        summary_ws = get_or_create_worksheet(client, settings, SUMMARY_TAB, rows=200, cols=12)
        summary_ws.clear()
        summary_ws.update(range_name="A1", values=[SUMMARY_HEADER] + rows)
        logger.info(f"Summary report written: {len(rows)} area(s)")
    except Exception as e:
        logger.error(f"Summary report failed (non-fatal): {e}", exc_info=True)
