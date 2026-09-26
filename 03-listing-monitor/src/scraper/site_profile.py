"""Site profile: everything specific to the target listing portal, in one place.

ADAPT TO THE TARGET PORTAL. Every value below is a neutral placeholder: URL
paths and query parameters, CSS selectors, page-text markers, status wording,
feature labels, postal-code format and date vocabulary. The scraper, parsers
and in-page JavaScript read them from here, so pointing the monitor at a real
site means editing this file only.

Regex patterns are plain strings valid in both Python `re` and JavaScript
`RegExp` (they are passed to the page via `page.evaluate(js, arg)`).
"""

from __future__ import annotations

import re

# --- URLs --------------------------------------------------------------------
# Search:  {base_url}/search?location=..&min_price=..&max_price=..&status=..&sort=..&page=N
# Listing: {base_url}/listing/{id}

SEARCH_PATH = "/search"
DETAIL_PATH = "/listing/"
LOCATION_PARAM = "location"      # repeated once per configured area
MIN_PRICE_PARAM = "min_price"
MAX_PRICE_PARAM = "max_price"
STATUS_PARAM = "status"          # repeated; used for the sold view
SORT_PARAM = "sort"
PAGE_PARAM = "page"

DEFAULT_SORT = "newest"
# Status-filter values that make up the "sold view".
DEFAULT_SOLD_STATUS_FILTER = ["under_offer", "sold"]

LISTING_ID_RE = re.escape(DETAIL_PATH) + r"(\d+)"

# --- Search results page -----------------------------------------------------

LISTING_LINK_SELECTOR = f'a[href*="{DETAIL_PATH}"]'
CARD_CONTAINER_SELECTOR = '[data-testid="listing-card"]'
CARD_SPEC_SELECTOR = 'li, [class*="specs"] span, [class*="feature"] span'
PROPERTY_TYPE_ATTR = "data-property-type"  # on the card container, e.g. "house"

# Page-text markers (compared lowercase). A verification page instead of results
# must never be read as "no listings"; RESULTS_MARKER is the positive signal that
# every real results page carries, even with 0 results.
BLOCK_MARKER = "please verify you are a human"
BLOCK_TITLE_MARKER = "just a moment"
RESULTS_MARKER = "homes for sale"

COOKIE_ACCEPT_TEXT = "Accept all"

CURRENCY_SYMBOL = "$"
PRICE_PATTERN = r"[$€£]\s*[\d.,]+"
STATUS_BADGE_PATTERN = r"Under offer|Sold"  # case-insensitive
# Placeholder postal format: 5 digits, then the city name.
POSTAL_CITY_PATTERN = r"(\d{5})\s+([A-Za-z\s]+?)(?=\s*[$€£]|\s*\d+\s*m|$)"
POSTAL_CODE_RE = r"(\d{5})\s+(.*)"
# Postal-code prefix lengths used by the report: district grouping and the
# coarse region lookup (reporting.POSTAL_PREFIX_TO_AREA).
POSTAL_DISTRICT_PREFIX_LEN = 3
POSTAL_REGION_PREFIX_LEN = 2

ROOM_KEYWORDS = ("room", "bedroom")
ROOMS_TEXT_RE = r"(\d+)\s*(?:bed)?rooms?"
RATING_RE = r"Rating[:\s]+([A-Z0-9+.]{1,4})"

# --- Listing status ----------------------------------------------------------

# Canonical status names as written to the sheet.
STATUS_AVAILABLE = "Available"
STATUS_UNDER_OFFER = "Under offer"
STATUS_SOLD = "Sold"

# Lowercase keywords used to recognise the portal's status wording.
SOLD_KEYWORD = "sold"
UNDER_OFFER_KEYWORD = "under offer"
AVAILABLE_KEYWORD = "available"

# --- Detail page -------------------------------------------------------------

NEIGHBOURHOOD_LINK_SELECTOR = ".listing-header > a"

# Labels the extractor writes itself (in addition to the <dt> labels).
NEIGHBOURHOOD_LABEL = "Neighbourhood"
DATE_LISTED_LABEL = "Date listed"
PRICE_PER_M2_LABEL = "Price per m²"

# case-insensitive
DATE_LISTED_PATTERN = r"Date listed[:\s]*(\d+\s+\w+\.?\s*\d{0,4}|\d+-\d+-\d+|today|yesterday)"
PRICE_PER_M2_PATTERN = r"([$€£]\s*[\d.,]+)\s*(?:per|/)\s*m²"  # case-insensitive

# <dt> label on the detail page -> ListingDetail field.
FEATURE_LABELS: dict[str, str] = {
    "Status": "status",
    "Year built": "build_year",
    "Property type": "building_type",
    "Construction": "construction_type",
    "Living area": "living_area_detail",
    "Floor area": "living_area_detail",
    "Volume": "volume_m3",
    "Rooms": "rooms",
    "Bathrooms": "bathrooms",
    "Floors": "floors",
    "Insulation": "insulation",
    "Heating": "heating",
    "Rating": "rating_detail",
    "Tenure": "ownership",
    "Garden": "garden",
    "Parking": "parking",
    NEIGHBOURHOOD_LABEL: "neighbourhood",
    PRICE_PER_M2_LABEL: "avg_price_per_m2",
    DATE_LISTED_LABEL: "date_listed",
}

# --- "Date listed" vocabulary ------------------------------------------------

TODAY_WORDS = ("today",)
YESTERDAY_WORDS = ("yesterday",)
# "N <unit> ago" -> days per unit (months approximated as 30 days).
RELATIVE_UNIT_DAYS: dict[str, int] = {
    "day": 1, "days": 1,
    "week": 7, "weeks": 7,
    "month": 30, "months": 30,
}
MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# --- Browser profile ---------------------------------------------------------

BROWSER_LOCALES = ("en-US",)
TIMEZONE_ID = "America/New_York"

# --- Arguments handed to the in-page extraction scripts ----------------------

CARD_JS_ARGS = {
    "linkSelector": LISTING_LINK_SELECTOR,
    "cardSelector": CARD_CONTAINER_SELECTOR,
    "specSelector": CARD_SPEC_SELECTOR,
    "propertyTypeAttr": PROPERTY_TYPE_ATTR,
    "pricePattern": PRICE_PATTERN,
    "statusPattern": STATUS_BADGE_PATTERN,
    "postalCityPattern": POSTAL_CITY_PATTERN,
}

DETAIL_JS_ARGS = {
    "neighbourhoodSelector": NEIGHBOURHOOD_LINK_SELECTOR,
    "neighbourhoodLabel": NEIGHBOURHOOD_LABEL,
    "dateListedPattern": DATE_LISTED_PATTERN,
    "dateListedLabel": DATE_LISTED_LABEL,
    "pricePerM2Pattern": PRICE_PER_M2_PATTERN,
    "pricePerM2Label": PRICE_PER_M2_LABEL,
}
