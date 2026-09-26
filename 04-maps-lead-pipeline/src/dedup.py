"""Deduplication: canonical keys + cross-source record merge.

Dedup anchor priority: registrable domain -> normalised phone -> name+postcode.
tldextract is pinned to its bundled suffix snapshot (`suffix_list_urls=()`) so
it never makes a network call.
"""

from __future__ import annotations

import re

import tldextract

from .models import MAX_EMAILS_PER_COMPANY, Company

_extract = tldextract.TLDExtract(suffix_list_urls=())


def registrable_domain(url_or_host: str) -> str:
    """example.nl from https://www.example.nl/contact (lowercased). '' if none."""
    if not url_or_host:
        return ""
    ext = _extract(url_or_host)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def normalize_phone(phone: str) -> str:
    """Canonical Dutch phone digits: strip formatting, map +31/0031 -> leading 0."""
    if not phone:
        return ""
    digits = re.sub(r"[^\d+]", "", phone)
    # Anchored: an international prefix is only a prefix. Unanchored, the old
    # replace ate the "0031" run *inside* ordinary numbers — 000-000 31 00
    # lost two digits, and the same number written
    # nationally and internationally normalised to two different keys.
    # `0?` absorbs the trunk digit in "+31 (0)20"; a leading "0031" is never an
    # area code (none begin with 0), so there is nothing else it can mean.
    digits = re.sub(r"^(?:\+31|0031)0?", "0", digits)
    return re.sub(r"\D", "", digits)


def classify_phone_type(phone: str) -> str:
    n = normalize_phone(phone)
    if not n:
        return "unknown"
    if n.startswith(("0900", "0906", "0909", "0800")):
        return "premium" if not n.startswith("0800") else "landline"
    if n.startswith("06"):
        return "mobile"
    if n.startswith("0"):
        return "landline"
    return "unknown"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _postcode_key(company: Company) -> str:
    return company.postcode.replace(" ", "").upper()


def dedup_key(company: Company) -> str:
    dom = company.domain or registrable_domain(company.website_url)
    if dom:
        return f"domain:{dom}"
    phone = normalize_phone(company.phone)
    if phone:
        return f"phone:{phone}"
    if company.google_place_id:
        return f"place:{company.google_place_id}"
    # Last resort. Nothing here is a shared identifier, so this key only has to
    # be stable across runs and distinct per company — hence the city, which
    # keeps two same-named firms apart when Places returned no postal_code.
    return f"name:{slugify(company.name)}|{_postcode_key(company)}|{slugify(company.city)}"


def identity_keys(company: Company) -> dict:
    """Every stable identifier for this company, for cross-source matching.

    Unlike `dedup_key` (which picks one preferred anchor), this exposes all
    identifiers at once so a company arriving via a different source — one with
    a website, another with only a phone — can still be matched on any shared
    identifier. Missing components are "" (callers must guard `<> ''` in SQL).
    """
    dom = registrable_domain(company.website_url or company.domain)
    phone = normalize_phone(company.phone)
    slug = slugify(company.name)
    postcode = _postcode_key(company)
    # Both halves are required. A name alone is not an identity: Places often
    # omits postal_code, and `slugify` widens the collision further ("A&B
    # Roofing" and "A B Roofing" both slug to "a-b-roofing"), so a
    # postcode-less key would merge unrelated firms in different cities.
    name = f"{slug}|{postcode}" if slug and postcode else ""
    return {"domain": dom, "phone": phone, "name": name}


def _prefer(a: str, b: str) -> str:
    return a if a else b


def _union_str(a: list[str], b: list[str]) -> list[str]:
    seen: list[str] = []
    for item in [*a, *b]:
        if item and item not in seen:
            seen.append(item)
    return seen


def merge_companies(existing: Company, new: Company) -> Company:
    """Fold `new` into `existing`, keeping the richest signal from each."""
    m = existing.model_copy(deep=True)

    # The canonical storage key is pinned to the existing row — a merge must
    # never adopt (or blank out) the incoming record's key.
    m.record_key = _prefer(existing.record_key, new.record_key)

    m.name = _prefer(m.name, new.name)
    m.website_url = _prefer(m.website_url, new.website_url)
    m.domain = _prefer(m.domain, new.domain)
    m.phone = _prefer(m.phone, new.phone)
    m.phone_type = _prefer(m.phone_type, new.phone_type)
    m.street = _prefer(m.street, new.street)
    m.postcode = _prefer(m.postcode, new.postcode)
    m.city = _prefer(m.city, new.city)
    m.province = _prefer(m.province, new.province)
    m.google_place_id = _prefer(m.google_place_id, new.google_place_id)
    m.contact_email = _prefer(m.contact_email, new.contact_email)

    if m.lat is None:
        m.lat, m.lng = new.lat, new.lng

    m.verticals_served = _union_str(m.verticals_served, new.verticals_served)
    m.certs = _union_str(m.certs, new.certs)

    # emails: union by address, keep highest confidence
    by_addr: dict[str, object] = {}
    for e in [*m.emails, *new.emails]:
        cur = by_addr.get(e.address)
        if cur is None or e.confidence > cur.confidence:  # type: ignore[union-attr]
            by_addr[e.address] = e
    # Two capped lists still union to twice the cap, so re-rank and re-cut on
    # the same rule the extractor uses.
    m.emails = sorted(  # type: ignore[assignment]
        by_addr.values(),
        key=lambda e: (e.kind == "role", e.confidence),  # type: ignore[union-attr]
        reverse=True,
    )[:MAX_EMAILS_PER_COMPANY]

    # keep the better review signal
    if (new.google_review_count or 0) > (m.google_review_count or 0):
        m.google_rating = new.google_rating
        m.google_review_count = new.google_review_count

    # provenance: union sources
    seen_src = {(s.name, s.source_id, s.url) for s in m.sources}
    for s in new.sources:
        if (s.name, s.source_id, s.url) not in seen_src:
            m.sources.append(s)

    # advance enrichment state if either is further along
    order = {"pending": 0, "failed": 1, "enriched": 2}
    if order.get(new.enrichment_status, 0) > order.get(m.enrichment_status, 0):
        m.enrichment_status = new.enrichment_status

    m.first_seen = min(m.first_seen, new.first_seen)
    m.last_seen = max(m.last_seen, new.last_seen)
    return m
