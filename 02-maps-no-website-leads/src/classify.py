"""Classify a business's web presence into a prospect tier.

"Has a website" is not binary. A business whose only listed link is a Facebook
page has no site — they are as much a prospect as one with a blank field, and
arguably a warmer one, since they have already shown they want to be findable.
Lumping those in with real websites would silently discard good leads.

Tiers, warmest first:
  none              — no link at all
  social_only       — a social profile standing in for a website
  ordering_platform — renting someone else's storefront (Menufy, Squareup, ...)
  real              — an actual site; not a prospect

Google's own auto-generated `business.site` pages sit in a grey area and are
configurable, because whether they count depends on what you are selling.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .models import WebsiteTier

logger = logging.getLogger(__name__)

SOCIAL_DOMAINS = {
    "facebook.com", "fb.com", "fb.me", "m.facebook.com",
    "instagram.com", "x.com", "twitter.com", "tiktok.com",
    "youtube.com", "wa.me", "api.whatsapp.com", "t.me",
    "snapchat.com", "pinterest.com", "linkedin.com", "nextdoor.com",
    # Link-in-bio services. A page of buttons pointing at social profiles is
    # not a website — these were landing in `real` during the first live run
    # and quietly costing leads.
    "linktr.ee", "linktree.com", "mssg.me", "beacons.ai", "bio.link",
    "carrd.co", "taplink.cc", "milkshake.app", "solo.to", "campsite.bio",
}

ORDERING_DOMAINS = {
    "menufy.com", "squareup.com", "square.site", "vagaro.com",
    "doordash.com", "ubereats.com", "grubhub.com", "opentable.com",
    "booksy.com", "treatwell.nl", "treatwell.com", "salonized.com",
    "thuisbezorgd.nl", "deliveroo.nl", "deliveroo.co.uk",
    "resengo.com", "formitable.com", "eversports.nl",
    "planity.com", "fresha.com", "setmore.com", "calendly.com",
    # Booking-app landing pages, observed during a live run.
    "apnt.app", "salonkee.nl", "salonkee.com", "shore.com",
    "simplybook.me", "acuityscheduling.com", "square.com",
    # Directory listings — the business is a row in someone else's database.
    "ivof.com", "yelp.com", "tripadvisor.com", "goudengids.nl",
    "telefoonboek.nl", "detelefoongids.nl",
}

# Google's free auto-generated Business Profile site. Barely a website — a single
# templated page the owner usually did not build and cannot really control. Best
# leads in the set, so default to treating them as prospects.
BUILDER_DOMAINS = {"business.site", "negocio.site", "company.site"}


def _host(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    if "://" not in u:
        u = f"https://{u}"
    try:
        host = (urlsplit(u).hostname or "").lower()
    except ValueError:
        return None
    return host.removeprefix("www.") or None


def _matches(host: str, domains: set[str]) -> bool:
    """True if host is one of `domains` or a subdomain of one.

    Substring matching would be wrong here — "notfacebook.com" must not match
    "facebook.com", and a naive `in` check would say it does.
    """
    return any(host == d or host.endswith(f".{d}") for d in domains)


def classify(
    url: str | None,
    *,
    builders_are_leads: bool = True,
    extra_social: set[str] | None = None,
    extra_ordering: set[str] | None = None,
) -> WebsiteTier:
    host = _host(url) if url else None
    if not host:
        return WebsiteTier.NONE

    if _matches(host, SOCIAL_DOMAINS | (extra_social or set())):
        return WebsiteTier.SOCIAL_ONLY
    if _matches(host, ORDERING_DOMAINS | (extra_ordering or set())):
        return WebsiteTier.ORDERING_PLATFORM
    if _matches(host, BUILDER_DOMAINS):
        return WebsiteTier.SOCIAL_ONLY if builders_are_leads else WebsiteTier.REAL

    return WebsiteTier.REAL
