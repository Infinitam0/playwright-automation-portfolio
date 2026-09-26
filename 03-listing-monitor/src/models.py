from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from src.scraper.parsers import STATUS_AVAILABLE, normalize_status


class ListingSummary(BaseModel):
    """Data extracted from a search result card."""

    listing_id: str
    url: str
    property_type: str = ""  # from the card, e.g. house / apartment
    street: str = ""
    postal_code: str = ""
    city: str = ""
    price_text: str = ""
    price: int | None = None
    living_area_m2: int | None = None
    plot_area_m2: int | None = None
    bedrooms: int | None = None
    rating: str = ""


class ListingDetail(BaseModel):
    """Extra data extracted from the detail page features section."""

    status: str = ""
    build_year: str = ""
    building_type: str = ""
    construction_type: str = ""
    living_area_detail: str = ""
    volume_m3: str = ""
    rooms: str = ""
    bathrooms: str = ""
    floors: str = ""
    insulation: str = ""
    heating: str = ""
    rating_detail: str = ""
    ownership: str = ""
    garden: str = ""
    parking: str = ""
    neighbourhood: str = ""
    avg_price_per_m2: str = ""
    date_listed: str = ""


class FullListing(BaseModel):
    """Combined search card + detail page data, ready for the sheet."""

    summary: ListingSummary
    detail: ListingDetail | None = None
    scraped_at: datetime

    def to_row(self) -> list[str]:
        """Convert to a flat list of strings matching the sheet columns."""
        s = self.summary
        d = self.detail or ListingDetail()
        scraped_iso = self.scraped_at.isoformat(timespec="seconds")
        # Lifecycle columns: on first insert, first_seen_at == scraped_at and
        # status_current seeds from the detail status (or STATUS_AVAILABLE if the
        # detail scrape yielded nothing). The remaining lifecycle fields are
        # populated later by the lifecycle sweep, so they start blank.
        status_current = normalize_status(d.status) or STATUS_AVAILABLE
        return [
            s.listing_id,
            s.url,
            s.property_type,
            s.street,
            s.postal_code,
            str(s.price) if s.price is not None else "",
            str(s.living_area_m2) if s.living_area_m2 is not None else "",
            str(s.plot_area_m2) if s.plot_area_m2 is not None else "",
            s.rating,
            d.status,
            d.build_year,
            d.building_type,
            d.construction_type,
            d.living_area_detail,
            d.volume_m3,
            d.rooms,
            d.bathrooms,
            d.floors,
            d.insulation,
            d.heating,
            d.rating_detail,
            d.ownership,
            d.garden,
            d.parking,
            d.neighbourhood,
            d.avg_price_per_m2,
            d.date_listed,
            scraped_iso,
            # --- lifecycle columns (AC-AH) ---
            scraped_iso,      # first_seen_at
            status_current,   # status_current
            "",               # status_changed_at
            "",               # closed_seen_at
            "",               # days_on_market
            "",               # sold_flag
        ]


SHEET_HEADERS = [
    "listing_id",
    "url",
    "property_type",
    "street",
    "postal_code",
    "price",
    "living_area_m2",
    "plot_area_m2",
    "rating",
    "status",
    "build_year",
    "building_type",
    "construction_type",
    "living_area_detail",
    "volume_m3",
    "rooms",
    "bathrooms",
    "floors",
    "insulation",
    "heating",
    "rating_detail",
    "ownership",
    "garden",
    "parking",
    "neighbourhood",
    "avg_price_per_m2",
    "date_listed",
    "scraped_at",
    # --- lifecycle tracking (appended; never reorder the columns above) ---
    "first_seen_at",       # ISO ts of first insert (== that row's scraped_at)
    "status_current",      # lifecycle-owned status (sweep-maintained)
    "status_changed_at",   # ISO ts status_current last changed
    "closed_seen_at",      # ISO ts the end-of-life event was first observed
    "days_on_market",      # materialized integer, written at close
    "sold_flag",           # TRUE if closed via sold/under-offer
]

# Index of the first lifecycle column (0-based). Columns before this are the
# original schema; sweep updates only ever touch columns at/after this index.
LIFECYCLE_START_INDEX = SHEET_HEADERS.index("first_seen_at")
