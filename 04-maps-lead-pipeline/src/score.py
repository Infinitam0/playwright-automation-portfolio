"""Fit scoring, exclusions + suppression.

Exclusions (competitor, aggregator, out-of-scope) and suppression
(do-not-contact) mark a company `is_suppressed` so it is never drafted or
exported for outreach.
"""

from __future__ import annotations

from .config import Settings
from .dedup import registrable_domain
from .models import Company

TARGET_COUNTRY = "NL"


def _exclusion_reason(company: Company, exclusions: dict) -> str:
    # A firm outside the target market is never a lead. Places queries near a
    # national border do return them (see google_places).
    if company.country and company.country != TARGET_COUNTRY:
        return f"out-of-scope-country:{company.country}"
    dom = company.domain or registrable_domain(company.website_url)
    if dom and dom in set(exclusions.get("excluded_domains", [])):
        return f"excluded-domain:{dom}"
    name = company.name.lower()
    for kw in exclusions.get("name_exclusion_keywords", []):
        if kw.lower() in name:
            return f"name-exclusion:{kw}"
    return ""


# Verdicts this pass re-derives from the live config on every run. A stale one
# must not outlive the config that produced it: removing a domain from
# exclusions.yml has to actually un-exclude the company.
#
# Deliberately NOT in this list:
#   suppressed-domain: / suppressed-email:  do-not-contact. Someone asked not to
#     be approached; a tidy-up of suppression.csv must not silently re-enable
#     outreach to them. Lifting one stays a deliberate act on the row.
#   negative-keyword:  evidence from the crawl. The page that proved it is not
#     stored, so this pass cannot re-derive it and must not discard it.
_DERIVED_REASON_PREFIXES = (
    "out-of-scope-country:",
    "excluded-domain:",
    "name-exclusion:",
)


def _suppression_reason(company: Company, suppression: dict) -> str:
    dom = company.domain or registrable_domain(company.website_url)
    if dom and dom in suppression.get("domains", set()):
        return f"suppressed-domain:{dom}"
    for e in company.emails:
        if e.address.lower() in suppression.get("emails", set()):
            return f"suppressed-email:{e.address}"
    return ""


def score_company(
    company: Company,
    *,
    exclusions: dict,
    suppression: dict,
    needed_verticals: list[str],
    province_tier: int,
    settings: Settings,
) -> Company:
    reasons: list[str] = []

    # Exclusion + suppression first (these veto outreach).
    if company.suppress_reason.startswith(_DERIVED_REASON_PREFIXES):
        company.suppress_reason = ""  # re-derived just below, or dropped
    reason = _exclusion_reason(company, exclusions) or _suppression_reason(
        company, suppression
    )
    company.suppress_reason = company.suppress_reason or reason

    # One source of truth: suppressed exactly when a live reason says so. A
    # write-only True flag would make a stale verdict permanent with no path
    # back except editing the DB.
    company.is_suppressed = bool(company.suppress_reason)

    score = 0
    if company.best_email():
        score += 25
        reasons.append("email")
    match = sorted(set(company.verticals_served) & set(needed_verticals))
    if match:
        score += 15
        reasons.append("vertical:" + ",".join(match))
    if company.certs:
        score += 20
        reasons.append("cert:" + ",".join(company.certs))

    rc = company.google_review_count or 0
    rating = company.google_rating or 0.0
    if rc and rating:
        quality = (rating / 5.0) * min(rc, 50) / 50.0
        bonus = round(quality * 10)
        score += bonus
        reasons.append(f"reviews:{rc}@{rating}(+{bonus})")

    if company.street and not company.is_po_box and company.phone_type in (
        "mobile",
        "landline",
    ):
        score += 5
        reasons.append("legit-contact")

    if province_tier == 1:
        score += settings.region_priority_weight
        reasons.append("region:tier1")

    company.fit_score = max(0, score)
    company.score_reasons = reasons

    if company.is_suppressed:
        company.priority_tier = "C"
    elif company.fit_score >= settings.tier_a_threshold:
        company.priority_tier = "A"
    elif company.fit_score >= settings.tier_b_threshold:
        company.priority_tier = "B"
    else:
        company.priority_tier = "C"
    return company
