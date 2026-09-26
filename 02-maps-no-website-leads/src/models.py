"""Data shapes passed between pipeline phases."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class WebsiteTier(enum.StrEnum):
    """How much of a prospect this business is.

    `none` and `social_only` are the product. `ordering_platform` is a warm
    third tier — they pay someone else for a storefront they don't control.
    """

    NONE = "none"
    SOCIAL_ONLY = "social_only"
    ORDERING_PLATFORM = "ordering_platform"
    REAL = "real"


@dataclass
class Place:
    """A result harvested from the search feed. Phase 1 output.

    Cheap to produce — this is everything obtainable without a detail page load.
    `fid` is what we dedupe on, before deciding whether to spend that load.
    """

    fid: str
    maps_url: str
    name: str | None = None
    cid: str | None = None
    tile: str | None = None


@dataclass
class PlaceDetail:
    """A verified business record. Phase 2 output.

    `website is None` is the claim the whole tool exists to make, so
    `extraction_misses` records which fields failed to resolve — a spike in
    misses for the website field means Google moved the JSON indices and every
    result since is untrustworthy.
    """

    fid: str
    maps_url: str
    name: str | None = None
    cid: str | None = None
    place_id: str | None = None
    website: str | None = None
    website_tier: WebsiteTier = WebsiteTier.NONE
    phone: str | None = None
    address: str | None = None
    category: str | None = None
    rating: float | None = None
    review_count: int | None = None
    plus_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    hours: str | None = None
    unclaimed: bool = False
    extraction_misses: list[str] = field(default_factory=list)

    @property
    def is_lead(self) -> bool:
        return self.website_tier is not WebsiteTier.REAL

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["website_tier"] = str(self.website_tier)
        d["extraction_misses"] = ", ".join(self.extraction_misses)
        return d
