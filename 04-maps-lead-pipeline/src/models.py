"""Pydantic v2 models for discovered companies + drafted emails.

A rich record plus a `to_row()` that flattens to the CSV/review header order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

VERTICALS = ("roofing", "plumbing", "electrical", "painting")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(BaseModel):
    """Where a candidate came from (provenance — one per discovery hit)."""

    name: str  # "places" | "maps" | "fixture"
    source_id: str = ""  # place_id / listing name
    url: str = ""
    scraped_at: datetime = Field(default_factory=_utcnow)


# A directory-style page (a wholesaler's branch list, an "our team" page) yields
# every address on it. Unbounded, that inflates the SQLite blob and the
# `all_emails` CSV column with people who were never the outreach target -- PII
# the operator neither asked for nor needs. The list is always ordered
# role-first then by confidence, with own-domain addresses boosted, so
# truncating keeps exactly the addresses worth reviewing. On a real run p95 was
# 3 and p99 was 9, so a cap of 10 trims under 1% of rows.
MAX_EMAILS_PER_COMPANY = 10


class EmailHit(BaseModel):
    address: str
    kind: str = "unknown"  # "role" (info@/contact@) | "personal" | "unknown"
    confidence: float = 0.5
    source_page: str = ""


class Company(BaseModel):
    """A candidate business lead, enriched and scored through the pipeline."""

    # Identity / web
    name: str
    website_url: str = ""
    domain: str = ""  # registrable domain, normalised (dedup anchor)

    # Contact
    emails: list[EmailHit] = []
    phone: str = ""
    phone_type: str = ""  # mobile | landline | premium | unknown

    # Location
    street: str = ""
    postcode: str = ""
    city: str = ""
    province: str = ""
    country: str = "NL"
    is_po_box: bool = False
    lat: Optional[float] = None
    lng: Optional[float] = None

    # Fit signals
    verticals_served: list[str] = []
    certs: list[str] = []
    google_rating: Optional[float] = None
    google_review_count: Optional[int] = None
    google_place_id: str = ""

    # Provenance + pipeline state
    sources: list[Source] = []
    enrichment_status: str = "pending"  # pending | enriched | failed
    fit_score: int = 0
    priority_tier: str = ""  # A | B | C
    score_reasons: list[str] = []
    record_key: str = ""  # pinned canonical storage key — never recomputed once set

    # Outreach
    contact_email: str = ""
    draft_subject: str = ""
    draft_body: str = ""
    draft_model: str = ""
    # needs_review = drafted but failed outreach.lint.lint_draft; such rows are
    # withheld from the CSV export (pipeline.UNSENDABLE_DRAFT_STATUSES).
    draft_status: str = "none"  # none | drafted | needs_review | approved | failed

    # Suppression
    is_suppressed: bool = False
    suppress_reason: str = ""

    # Timestamps
    first_seen: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)

    # --- convenience --------------------------------------------------
    def best_email(self) -> str:
        """Preferred contact address: role addresses first, then confidence."""
        if not self.emails:
            return ""
        ranked = sorted(
            self.emails,
            key=lambda e: (e.kind == "role", e.confidence),
            reverse=True,
        )
        return ranked[0].address

    @property
    def source_names(self) -> str:
        return "|".join(sorted({s.name for s in self.sources}))

    def to_row(self) -> list[str]:
        """Flat strings matching CSV_HEADERS (the human review surface)."""
        return [
            self.name,
            self.city,
            self.province,
            self.website_url,
            self.contact_email or self.best_email(),
            "; ".join(e.address for e in self.emails),
            self.phone,
            self.phone_type,
            "; ".join(self.verticals_served),
            "; ".join(self.certs),
            _num(self.google_rating),
            _num(self.google_review_count),
            str(self.fit_score),
            self.priority_tier,
            self.draft_status,
            self.draft_subject,
            self.draft_body,
            self.source_names,
            self.enrichment_status,
            self.last_seen.isoformat(timespec="seconds"),
        ]


def _num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") or "0"
    return str(v)


CSV_HEADERS = [
    "name",
    "city",
    "province",
    "website",
    "contact_email",
    "all_emails",
    "phone",
    "phone_type",
    "verticals",
    "certs",
    "google_rating",
    "google_reviews",
    "fit_score",
    "priority_tier",
    "draft_status",
    "draft_subject",
    "draft_body",
    "sources",
    "enrichment_status",
    "last_seen",
]


class RawCandidate(BaseModel):
    """Thin discovery output before enrichment. Discovery adapters yield these."""

    name: str
    website_url: str = ""
    phone: str = ""
    street: str = ""
    postcode: str = ""
    city: str = ""
    province: str = ""
    country: str = ""  # ISO-2 from the source; "" when the source cannot say
    google_rating: Optional[float] = None
    google_review_count: Optional[int] = None
    google_place_id: str = ""
    source: Source

    def to_company(self, vertical_hint: str = "") -> Company:
        verticals = [vertical_hint] if vertical_hint in VERTICALS else []
        return Company(
            name=self.name,
            website_url=self.website_url,
            phone=self.phone,
            street=self.street,
            postcode=self.postcode,
            city=self.city,
            province=self.province,
            country=self.country or "NL",
            google_rating=self.google_rating,
            google_review_count=self.google_review_count,
            google_place_id=self.google_place_id,
            verticals_served=verticals,
            sources=[self.source],
        )
