"""PHASE 2 — open a place page and read the embedded JSON payload.

Why JSON and not the DOM: Google ships the full place record to the page as a
guarded JSON string under `window.APP_INITIALIZATION_STATE`. Reading it is immune
to CSS class rotation and to locale (aria-label selectors return nothing outside
en-US). The DOM strategies in selectors.py exist only as fallbacks.

Two-stage lookup, because both stages move independently:
  1. Find the guarded payload — its location changed from state[3][6] to
     state[3]["qg"][2], so we scan rather than index.
  2. Find the place record inside it — moved from [6] to [0][1][0][14], so we
     validate candidates by shape instead of trusting a path.
Field offsets *within* the record have been stable for years.

The website field is the product. Every other field is context for the pitch.
"""

from __future__ import annotations

import json
import logging

from ..models import PlaceDetail
from . import selectors as S
from .consent import safe_goto

logger = logging.getLogger(__name__)


class BlobUnavailable(Exception):
    """The page loaded but carried no usable place record.

    Deliberately distinct from "this business has no website" — conflating the
    two would manufacture leads out of failures, which is the worst bug this tool
    could have. Callers must never treat this as a no-website result.
    """


async def fetch_record(page, url: str, *, timeout_ms: int = 45_000) -> list:
    """Navigate to a place URL and return its place record."""
    await safe_goto(page, url, timeout_ms=timeout_ms)

    # The payload lands well after domcontentloaded. Poll for it — an early read
    # returns nothing, which is indistinguishable from a business having no data.
    try:
        await page.wait_for_function(S.HAS_BLOB_JS, timeout=timeout_ms)
    except Exception as e:  # noqa: BLE001
        raise BlobUnavailable(f"payload never appeared at {url}: {e}") from e

    raw_blobs = await page.evaluate(S.EXTRACT_BLOBS_JS)
    if not raw_blobs:
        raise BlobUnavailable(f"no guarded payload in page state at {url}")

    for raw in raw_blobs:
        try:
            blob = json.loads(raw[len(S.XSSI_GUARD) :])
        except json.JSONDecodeError:
            continue
        record, path = S.find_place_record(blob)
        if record is None:
            continue
        if list(path) not in [list(p) for p in S.RECORD_ROOTS]:
            logger.warning(
                "PAYLOAD DRIFT: place record found at %s, not a known root. "
                "Add it to selectors.RECORD_ROOTS. (%s)",
                path,
                url,
            )
        return record

    raise BlobUnavailable(f"no place record in any payload at {url}")


def _resolve(record, strategies, field_name: str, misses: list[str]):
    """Try each JSON strategy in order; record a miss if all fail.

    DOM fallbacks need a live page and are handled by the caller.
    """
    for strat in strategies:
        if isinstance(strat, S.JsonPath):
            val = strat.extract(record)
            if val is not None:
                return val
    misses.append(field_name)
    return None


def parse_detail(record: list, maps_url: str, fid: str | None = None) -> PlaceDetail:
    """Pure function: record -> PlaceDetail.

    No network, so it is unit-testable against saved fixtures — which is what
    makes an index-rot break fast to diagnose and fix.
    """
    misses: list[str] = []

    def num(v, cast):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    # The record's own feature id beats one regexed out of the URL.
    record_fid = _resolve(record, S.RECORD_FID, "fid", misses)
    fid = (record_fid if isinstance(record_fid, str) else None) or fid or S.extract_fid(maps_url) or ""

    website = _resolve(record, S.WEBSITE, "website", misses)
    # A missing website is a legitimate result, not an extraction failure, so it
    # must not land in `misses` — see WEBSITE_DOMAIN cross-check below.
    if website is None:
        misses.remove("website")
    domain = _resolve(record, S.WEBSITE_DOMAIN, "website_domain", [])

    # A bare domain with no full URL still means they have a site. Not treating
    # it as one would put a business with a website on the pitch list.
    if not isinstance(website, str) and isinstance(domain, str) and domain.strip():
        website = f"https://{domain.strip()}"

    return PlaceDetail(
        fid=fid.lower(),
        maps_url=maps_url,
        name=_resolve(record, S.NAME, "name", misses),
        cid=S.fid_to_cid(fid),
        place_id=_resolve(record, S.PLACE_ID, "place_id", misses),
        website=website if isinstance(website, str) else None,
        phone=_resolve(record, S.PHONE, "phone", misses),
        address=_resolve(record, S.ADDRESS, "address", misses),
        category=_resolve(record, S.CATEGORY, "category", misses),
        rating=num(_resolve(record, S.RATING, "rating", misses), float),
        review_count=num(_resolve(record, S.REVIEW_COUNT, "review_count", misses), int),
        plus_code=_resolve(record, S.PLUS_CODE, "plus_code", misses),
        latitude=num(_resolve(record, S.LATITUDE, "latitude", misses), float),
        longitude=num(_resolve(record, S.LONGITUDE, "longitude", misses), float),
        extraction_misses=misses,
    )


async def fetch_detail(page, maps_url: str, fid: str | None = None) -> PlaceDetail:
    record = await fetch_record(page, maps_url)
    detail = parse_detail(record, maps_url, fid)

    # "No website" is the claim we are selling, so check the DOM before believing
    # it. A hit here means the JSON offsets moved — that is a loud problem, not a
    # quiet fallback, because everything scraped since is suspect.
    if detail.website is None:
        for strat in S.WEBSITE:
            if not isinstance(strat, S.Dom):
                continue
            try:
                el = await page.query_selector(strat.selector)
                if not el:
                    continue
                val = (
                    await el.get_attribute(strat.attr)
                    if strat.attr
                    else await el.inner_text()
                )
                if val and val.strip():
                    logger.warning(
                        "OFFSET ROT: JSON website path missed but DOM %s found %r at %s "
                        "— re-verify selectors.WEBSITE before trusting this run",
                        strat.selector,
                        val,
                        maps_url,
                    )
                    detail.website = val.strip()
                    break
            except Exception:  # noqa: BLE001
                continue

    if detail.extraction_misses:
        logger.debug("misses %s at %s", detail.extraction_misses, maps_url)
    return detail
