"""PHASE 1 — harvest place URLs from a search results feed.

Cheap by design. This phase only collects identity (feature id + URL + name);
the website field costs a page load per business and is phase 2's job.

The one thing that matters here besides coverage: extracting the feature id from
the href so duplicates across overlapping grid tiles are dropped BEFORE they cost
a detail load. That is what makes a city-wide run affordable.
"""

from __future__ import annotations

import logging

from ..models import Place
from . import selectors as S
from .consent import safe_goto

logger = logging.getLogger(__name__)


def build_search_url(query: str, lat: float | None = None, lng: float | None = None,
                     zoom: int = 15) -> str:
    """A Maps search URL, optionally anchored to a map position.

    The @lat,lng,zoom anchor is what makes grid tiling work — it moves the
    viewport, and Maps ranks results by proximity to it.
    """
    q = query.strip().replace(" ", "+")
    base = f"https://www.google.com/maps/search/{q}"
    if lat is not None and lng is not None:
        base += f"/@{lat:.7f},{lng:.7f},{zoom}z"
    return base


async def _count_results(page) -> int:
    try:
        return await page.eval_on_selector_all(
            f'{S.FEED} a[href*="/maps/place/"]', "els => els.length"
        )
    except Exception:  # noqa: BLE001
        return 0


async def _scroll_feed(page, max_scrolls: int = 60) -> int:
    """Scroll until no new results arrive.

    Stops on the count of result links rather than scrollHeight. Height can
    plateau for a moment while the next batch is still in flight, and treating
    that as the end truncated tiles badly on the first live sweep.

    Waits BEFORE counting, so every plateau round represents real elapsed quiet
    rather than an instant re-read of the same DOM. The end-of-list text is
    deliberately not used — it is locale-dependent and would never match here.
    """
    delay_ms = S.SCROLL_BACKOFF_START_MS
    last_count = -1
    plateaus = 0

    for i in range(max_scrolls):
        try:
            await page.eval_on_selector(S.FEED, "el => { el.scrollTop = el.scrollHeight; }")
        except Exception as e:  # noqa: BLE001
            logger.debug("feed scroll stopped at round %d: %s", i, e)
            break

        await page.wait_for_timeout(delay_ms)
        count = await _count_results(page)

        if count == last_count:
            plateaus += 1
            if plateaus >= S.SCROLL_PLATEAU_ROUNDS:
                break
        else:
            plateaus = 0
            last_count = count
            if count >= S.RESULT_CAP:
                break  # Google will not serve more for this query

        delay_ms = min(int(delay_ms * S.SCROLL_BACKOFF_FACTOR), S.SCROLL_BACKOFF_MAX_MS)

    return max(last_count, 0)


async def harvest(page, query: str, *, lat: float | None = None,
                  lng: float | None = None, zoom: int = 15,
                  tile: str | None = None) -> list[Place]:
    """Run one search and return the deduped places found in its feed."""
    url = build_search_url(query, lat, lng, zoom)
    await safe_goto(page, url)

    # Wait for an actual result card, not merely the feed container. The
    # container appears first and empty; scrolling it at that point harvests
    # whatever happens to have rendered and calls the tile done.
    try:
        await page.wait_for_selector(
            f'{S.FEED} a[href*="/maps/place/"]', timeout=20_000
        )
    except Exception:  # noqa: BLE001
        # A query with one obvious answer skips the feed and lands on the place.
        if S.SINGLE_PLACE_URL_MARKER in page.url:
            fid = S.extract_fid(page.url)
            if fid:
                logger.info("'%s' resolved to a single place", query)
                return [Place(fid=fid, maps_url=page.url, cid=S.fid_to_cid(fid), tile=tile)]
        logger.info("'%s' @%s produced no results", query, tile or "default")
        return []

    await _scroll_feed(page)

    # Union of both link strategies: one rotating away should not be an outage.
    anchors = []
    for sel in S.FEED_PLACE_LINKS:
        try:
            anchors.extend(await page.query_selector_all(sel))
        except Exception:  # noqa: BLE001
            continue

    places: dict[str, Place] = {}
    skipped = 0
    for a in anchors:
        href = await a.get_attribute("href") or ""
        if "/maps/place/" not in href:
            continue
        fid = S.extract_fid(href)
        if not fid:
            skipped += 1
            continue
        if fid in places:
            continue
        places[fid] = Place(
            fid=fid,
            maps_url=href,
            name=(await a.get_attribute("aria-label") or "").strip() or None,
            cid=S.fid_to_cid(fid),
            tile=tile,
        )

    if skipped:
        logger.debug("%d feed links had no feature id", skipped)

    n = len(places)
    if n >= S.SUBDIVIDE_THRESHOLD:
        note = "  [AT CAP — subdividing]"
    elif n <= S.SUSPICIOUS_YIELD:
        # Genuinely sparse tiles exist, but this is also exactly what a truncated
        # scroll looks like. Say so, so it cannot hide in a 6-hour log again.
        note = "  [low yield — sparse area, or scroll cut short?]"
    else:
        note = ""

    logger.info("'%s' @%s -> %d places%s", query, tile or "default", n, note)
    return list(places.values())
