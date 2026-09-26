"""Parsing utilities for real-estate listing data.

Portal-specific wording and formats come from `site_profile`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from src.scraper import site_profile as P


def parse_price(text: str) -> int | None:
    """Extract a whole-currency amount from strings like '$325,000' or '$325,000.00'.

    Any thousands separator (',', '.', space) is accepted; a trailing 1-2 digit
    decimal part is dropped.
    """
    if not text:
        return None
    match = re.search(r"\d[\d.,\s]*", text)
    if not match:
        return None
    number = re.sub(r"[.,]\d{1,2}$", "", match.group(0).strip())
    digits = re.sub(r"\D", "", number)
    return int(digits) if digits else None


def parse_area(text: str) -> int | None:
    """Extract integer m² from strings like '120 m²' or '120m2'."""
    if not text:
        return None
    match = re.search(r"(\d+)\s*m", text)
    if match:
        return int(match.group(1))
    return None


def parse_bedrooms(text: str) -> int | None:
    """Extract bedroom count from strings like '3 rooms' or just '3'."""
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def extract_listing_id(url: str) -> str:
    """Extract the numeric listing ID from a detail URL ({base_url}/listing/{id})."""
    match = re.search(P.LISTING_ID_RE, url)
    if match:
        return match.group(1)
    # Fallback: use the whole URL as ID
    return url


def parse_postal_code(text: str) -> tuple[str, str]:
    """Split '<postal code> <city>' into (postal_code, city).

    The postal-code format is `site_profile.POSTAL_CODE_RE`.
    Returns (postal_code, city). If parsing fails, returns ('', text).
    """
    if not text:
        return ("", "")
    match = re.match(P.POSTAL_CODE_RE, text.strip())
    if match:
        return (match.group(1).strip(), match.group(2).strip())
    return ("", text.strip())


# --- Lifecycle parsing helpers ---

# Canonical lifecycle statuses. The first three are the portal's own states
# (see site_profile); the "Closed-*" values are assigned by the lifecycle sweep
# when a listing leaves the active inventory and we classify why.
STATUS_AVAILABLE = P.STATUS_AVAILABLE
STATUS_UNDER_BID = P.STATUS_UNDER_OFFER
STATUS_SOLD = P.STATUS_SOLD
STATUS_CLOSED_WITHDRAWN = "Closed-Withdrawn"
STATUS_CLOSED_PRICE_CHANGE = "Closed-PriceChange"
STATUS_CLOSED_UNKNOWN = "Closed-Unknown"

# Statuses that count as a sale — the sell signal we measure.
SOLD_STATUSES = frozenset({STATUS_SOLD})


def normalize_status(text: str) -> str:
    """Canonicalize a raw portal status / card badge string.

    Map the portal's status wording (any case, extra words allowed) to the
    canonical STATUS_* constants. Empty input
    returns "" so callers can apply their own default. An unrecognized
    non-empty value is returned stripped, so we never silently drop information
    we don't yet model.
    """
    if not text:
        return ""
    t = text.strip()
    low = t.lower()
    if P.UNDER_OFFER_KEYWORD in low:
        return STATUS_UNDER_BID
    if P.SOLD_KEYWORD in low:
        return STATUS_SOLD
    if P.AVAILABLE_KEYWORD in low:
        return STATUS_AVAILABLE
    return t


# Longest unit first so "days" is matched as a whole word before "day".
_RELATIVE_RE = re.compile(
    r"(\d+)\s*(" + "|".join(sorted(map(re.escape, P.RELATIVE_UNIT_DAYS), key=len, reverse=True)) + ")"
)


def parse_listed_date(text: str, today: date | None = None) -> date | None:
    """Parse a "date listed" value into a date.

    Handles (vocabulary from site_profile):
      - month names: "21 June 2024", "21 Jun. 2024", "1 May 2025"
      - numeric: "2024-06-21", "21-06-2024", "21/06/2024"
      - relative: "today", "yesterday", "3 days ago", "2 weeks ago", "1 month ago"
    Anything vaguer ("3+ months") returns None (the caller falls back to
    first_seen_at and flags the start date as estimated).

    `today` defaults to date.today(); pass it explicitly for deterministic tests.
    """
    if not text:
        return None
    ref = today or date.today()
    raw = text.strip()
    low = raw.lower()

    # Relative day anchors
    if low in P.TODAY_WORDS:
        return ref
    if low in P.YESTERDAY_WORDS:
        return ref - timedelta(days=1)

    # "3+ months" is deliberately vague — no reliable date
    if "+" in low:
        return None

    # Relative "N day(s) / week(s) / month(s) ago"
    rel = _RELATIVE_RE.match(low)
    if rel:
        n = int(rel.group(1))
        return ref - timedelta(days=P.RELATIVE_UNIT_DAYS[rel.group(2)] * n)

    # ISO: 2024-06-21
    iso = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    # Numeric dd-mm-yyyy or dd/mm/yyyy
    dmy = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", raw)
    if dmy:
        year = int(dmy.group(3))
        if year < 100:
            year += 2000
        return _safe_date(year, int(dmy.group(2)), int(dmy.group(1)))

    # Day month-name [year]: "21 June 2024" / "21 Jun. 2024" / "21 June"
    named = re.match(r"(\d{1,2})\s+([A-Za-z.]+)\.?\s*(\d{4})?", raw)
    if named:
        month = P.MONTHS.get(named.group(2).strip(". ").lower())
        if month:
            year = int(named.group(3)) if named.group(3) else ref.year
            return _safe_date(year, month, int(named.group(1)))

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Build a date, returning None on out-of-range components."""
    try:
        return date(year, month, day)
    except ValueError:
        return None
