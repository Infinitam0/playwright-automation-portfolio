"""Google Sheets storage: read known IDs, append new rows."""

from __future__ import annotations

import logging

import gspread
from google.oauth2.service_account import Credentials

from src.config import Settings
from src.models import SHEET_HEADERS, FullListing, ListingDetail

logger = logging.getLogger(__name__)

# 0-based column indices for lifecycle fields, derived from SHEET_HEADERS so
# they stay correct if the schema is extended (never reorder existing columns).
_IDX_LISTING_ID = SHEET_HEADERS.index("listing_id")
_IDX_DATE_LISTED = SHEET_HEADERS.index("date_listed")
_IDX_FIRST_SEEN = SHEET_HEADERS.index("first_seen_at")
_IDX_STATUS_CURRENT = SHEET_HEADERS.index("status_current")
_IDX_STATUS_CHANGED = SHEET_HEADERS.index("status_changed_at")
_IDX_CLOSED_SEEN = SHEET_HEADERS.index("closed_seen_at")
_IDX_DAYS_ON_MARKET = SHEET_HEADERS.index("days_on_market")
_IDX_SOLD_FLAG = SHEET_HEADERS.index("sold_flag")


def _a1_col(index_zero_based: int) -> str:
    """Convert a 0-based column index to an A1 column letter (0 -> A, 28 -> AC)."""
    n = index_zero_based + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# A1 range covering status_current..sold_flag for one row (the sweep-owned
# columns). first_seen_at is written once at insert and never overwritten.
_LIFECYCLE_WRITE_START_COL = _a1_col(_IDX_STATUS_CURRENT)
_LIFECYCLE_WRITE_END_COL = _a1_col(_IDX_SOLD_FLAG)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_client(settings: Settings) -> gspread.Client:
    """Create an authenticated gspread client."""
    creds = Credentials.from_service_account_file(
        str(settings.google_credentials_path),
        scopes=SCOPES,
    )
    return gspread.authorize(creds)


def get_worksheet(
    client: gspread.Client, settings: Settings
) -> gspread.Worksheet:
    """Open the spreadsheet and return the first worksheet (the listings tab)."""
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    return spreadsheet.sheet1


def get_or_create_worksheet(
    client: gspread.Client,
    settings: Settings,
    title: str,
    rows: int = 1000,
    cols: int = 26,
) -> gspread.Worksheet:
    """Return the worksheet (tab) named `title`, creating it if absent."""
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        logger.info(f"Creating worksheet tab '{title}'")
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def ensure_headers(worksheet: gspread.Worksheet) -> None:
    """Ensure row 1 has the correct headers. Write them if empty."""
    first_row = worksheet.row_values(1)
    if not first_row or first_row[0] != SHEET_HEADERS[0]:
        logger.info("Writing sheet headers")
        worksheet.update(
            range_name="A1",
            values=[SHEET_HEADERS],
        )


def load_known_ids(worksheet: gspread.Worksheet) -> set[str]:
    """Load all known listing IDs from column A (skipping header)."""
    col_values = worksheet.col_values(1)
    # Skip header row
    ids = {v.strip() for v in col_values[1:] if v.strip()}
    logger.info(f"Loaded {len(ids)} known IDs from sheet")
    return ids


def find_incomplete_rows(worksheet: gspread.Worksheet) -> list[dict]:
    """Find rows with a URL but missing detail data (columns J-AA).

    Returns list of dicts with keys: row (1-indexed), listing_id, url.
    """
    all_rows = worksheet.get_all_values()
    if len(all_rows) <= 1:
        return []

    incomplete = []
    for row_idx, row in enumerate(all_rows[1:], start=2):
        listing_id = row[0] if len(row) > 0 else ""
        url = row[1] if len(row) > 1 else ""
        if not listing_id or not url:
            continue

        # Detail columns are indices 9-26 (0-based) = columns J-AA
        detail_values = row[9:27] if len(row) >= 27 else row[9:]
        non_empty = sum(1 for v in detail_values if v.strip())

        if non_empty <= 2:
            incomplete.append({
                "row": row_idx,
                "listing_id": listing_id,
                "url": url,
            })

    return incomplete


def update_row_details(
    worksheet: gspread.Worksheet,
    row_number: int,
    detail: ListingDetail,
) -> None:
    """Update the detail columns (J-AA) for a specific row.

    Uses batch update to minimize API calls.
    """

    detail_values = [
        detail.status,
        detail.build_year,
        detail.building_type,
        detail.construction_type,
        detail.living_area_detail,
        detail.volume_m3,
        detail.rooms,
        detail.bathrooms,
        detail.floors,
        detail.insulation,
        detail.heating,
        detail.rating_detail,
        detail.ownership,
        detail.garden,
        detail.parking,
        detail.neighbourhood,
        detail.avg_price_per_m2,
        detail.date_listed,
    ]

    # Columns J-AA = columns 10-27 (1-indexed)
    # In A1 notation: J{row}:AA{row}
    range_name = f"J{row_number}:AA{row_number}"
    worksheet.update(range_name=range_name, values=[detail_values])
    logger.debug(f"Updated row {row_number} detail columns")


def append_listings(
    worksheet: gspread.Worksheet,
    listings: list[FullListing],
) -> int:
    """Insert new listings at row 2 (just below headers), newest first.

    Returns the number of rows inserted.
    """
    if not listings:
        return 0

    rows = [listing.to_row() for listing in listings]

    # Insert at row 2 so newest appear at the top
    worksheet.insert_rows(rows, row=2)
    logger.info(f"Inserted {len(rows)} new rows into sheet")
    return len(rows)


def _cell(row: list[str], idx: int) -> str:
    """Safely read a (possibly short) row at a 0-based column index."""
    return row[idx].strip() if len(row) > idx and row[idx] else ""


def load_active_lifecycle(worksheet: gspread.Worksheet) -> dict[str, dict]:
    """Load lifecycle state for listings still being tracked.

    A listing is "active" for the sweep until its `closed_seen_at` is set, after
    which it is terminal and ignored. Reads the whole sheet in one call.

    Returns a dict keyed by listing_id with: row (1-indexed), status_current,
    first_seen_at, date_listed. Later duplicate listing_ids (lower rows) do not
    overwrite the first (top-most / newest) occurrence.
    """
    all_rows = worksheet.get_all_values()
    if len(all_rows) <= 1:
        return {}

    active: dict[str, dict] = {}
    for row_idx, row in enumerate(all_rows[1:], start=2):
        listing_id = _cell(row, _IDX_LISTING_ID)
        if not listing_id or listing_id in active:
            continue
        # Skip rows already closed (terminal).
        if _cell(row, _IDX_CLOSED_SEEN):
            continue
        active[listing_id] = {
            "row": row_idx,
            "status_current": _cell(row, _IDX_STATUS_CURRENT),
            "first_seen_at": _cell(row, _IDX_FIRST_SEEN) or _cell(row, _IDX_DATE_LISTED),
            "date_listed": _cell(row, _IDX_DATE_LISTED),
        }
    logger.info(f"Loaded {len(active)} active (open) listings for lifecycle sweep")
    return active


def batch_update_lifecycle(
    worksheet: gspread.Worksheet,
    updates: list[tuple[int, list[str]]],
) -> int:
    """Write sweep-owned lifecycle columns (status_current..sold_flag) for many
    rows in a single API call.

    Each update is (row_number, [status_current, status_changed_at,
    closed_seen_at, days_on_market, sold_flag]). Returns the number of rows
    written. Using one batch_update keeps us well within gspread's write quota
    even when a sweep changes dozens of rows.
    """
    if not updates:
        return 0

    body = [
        {
            "range": f"{_LIFECYCLE_WRITE_START_COL}{row}:{_LIFECYCLE_WRITE_END_COL}{row}",
            "values": [values],
        }
        for row, values in updates
    ]
    worksheet.batch_update(body)
    logger.info(f"Lifecycle sweep updated {len(updates)} row(s)")
    return len(updates)
