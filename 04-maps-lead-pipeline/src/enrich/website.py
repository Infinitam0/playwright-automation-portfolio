"""Crawl a company website and extract email / certs / verticals / phone.

`fetch` is injected (async url -> html) so this runs offline in tests. In the
pipeline it is `HttpClient.get_text`, which enforces robots.txt + per-host
rate limiting.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable
from urllib.parse import urljoin

from ..config import Settings
from ..dedup import classify_phone_type, registrable_domain
from ..models import MAX_EMAILS_PER_COMPANY, Company, EmailHit
from . import html_to_text
from .cert_detect import detect_certs
from .email_extract import extract_emails
from .vertical_match import match_verticals, negative_keyword_hit

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], Awaitable[str]]

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
# URL fragments of contact/about pages. The Dutch slugs ("over-ons" = about us,
# "contactgegevens" = contact details) are what target sites actually use.
_CONTACT_HINTS = (
    "contact", "over-ons", "overons", "about", "team", "service",
    "contactgegevens", "/over",
)
# Dutch phone numbers: +31 / 0031 / trunk 0, then 9 digits with optional separators.
_PHONE_RE = re.compile(r"(?:(?:\+31|0031)\s?|0)(?:\d[\s\-]?){8,9}\d")


async def enrich_company(
    company: Company,
    *,
    fetch: Fetcher,
    verticals_cfg: dict,
    settings: Settings,
    # `out_of_scope_keywords` from exclusions.yml: wrong-segment signals
    # (e.g. wholesale-only). NOT generic site vocabulary.
    negative_keywords: list[str] | None = None,
    # `segment_signals` from exclusions.yml: evidence the company also serves
    # the target segment, which can cancel a negative hit (see vertical_match).
    segment_signals: list[str] | None = None,
) -> Company:
    base = company.website_url
    if not base:
        company.enrichment_status = "failed"
        return company
    if not base.startswith("http"):
        base = "https://" + base
    domain = registrable_domain(base)
    company.domain = company.domain or domain

    try:
        home = await fetch(base)
    except Exception as e:  # noqa: BLE001
        logger.info(f"enrich: homepage fetch failed for {base}: {e}")
        company.enrichment_status = "failed"
        return company

    pages: list[tuple[str, str]] = [(base, home)]
    for url in _internal_links(home, base, domain)[: settings.enrich_max_pages_per_site - 1]:
        try:
            pages.append((url, await fetch(url)))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"enrich: sub-page fetch failed {url}: {e}")

    all_text = "\n".join(html_to_text(h) for _, h in pages)

    emails: list[EmailHit] = []
    for url, h in pages:
        emails.extend(extract_emails(h, source_page=url))
    company.emails = _dedupe_prefer_domain(emails, domain)
    company.contact_email = company.best_email()

    company.certs = detect_certs(all_text) or company.certs
    company.verticals_served = _union(
        company.verticals_served, match_verticals(all_text, verticals_cfg)
    )

    if not company.phone:
        m = _PHONE_RE.search(all_text)
        if m:
            company.phone = m.group(0).strip()
    company.phone_type = classify_phone_type(company.phone)
    company.is_po_box = "postbus" in (company.street or "").lower()  # Dutch for "PO box"

    if negative_keywords:
        hit = negative_keyword_hit(
            all_text, company.name, negative_keywords, segment_signals
        )
        if hit:
            company.is_suppressed = True
            company.suppress_reason = f"negative-keyword:{hit}"

    company.enrichment_status = "enriched"
    return company


def _internal_links(html: str, base: str, domain: str) -> list[str]:
    out, seen = [], set()
    for href in _HREF_RE.findall(html):
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = urljoin(base, href)
        if registrable_domain(url) != domain:
            continue
        if any(hint in url.lower() for hint in _CONTACT_HINTS) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _dedupe_prefer_domain(hits: list[EmailHit], domain: str) -> list[EmailHit]:
    by: dict[str, EmailHit] = {}
    for h in hits:
        cur = by.get(h.address)
        if cur is None or h.confidence > cur.confidence:
            by[h.address] = h
    result = list(by.values())
    for h in result:
        if domain and registrable_domain(h.address.split("@")[-1]) == domain:
            h.confidence = min(1.0, h.confidence + 0.15)
    ranked = sorted(result, key=lambda e: (e.kind == "role", e.confidence), reverse=True)
    return ranked[:MAX_EMAILS_PER_COMPANY]


def _union(a: list[str], b: list[str]) -> list[str]:
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return out
