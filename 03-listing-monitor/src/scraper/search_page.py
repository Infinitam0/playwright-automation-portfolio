"""Search results pagination and listing card extraction."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from playwright.async_api import Page

if TYPE_CHECKING:
    from src.lifecycle import SweepObservation

from src.config import Settings
from src.models import ListingSummary
from src.scraper import site_profile as P
from src.scraper.browser import dismiss_cookie_consent
from src.scraper.parsers import (
    STATUS_SOLD,
    extract_listing_id,
    normalize_status,
    parse_area,
    parse_bedrooms,
    parse_postal_code,
    parse_price,
)
from src.scraper.timing import jittered_delay

logger = logging.getLogger(__name__)

# A page can come back as a verification page instead of results (HTTP 200,
# zero listing links). The site_profile markers tell that apart from a genuinely
# empty result set. RESULTS_MARKER is the positive signal — every real results
# page carries the "N homes for sale" count, including a zero-result page and a
# page past the end of the inventory; a verification page carries none.
BLOCK_MARKER = P.BLOCK_MARKER
BLOCK_TITLE_MARKER = P.BLOCK_TITLE_MARKER
RESULTS_MARKER = P.RESULTS_MARKER
# Refusals that come as an error status. Corroboration only — a verification
# page is HTTP 200, so status can never be the primary signal.
BLOCK_STATUSES = frozenset({403, 429, 503})


class BlockedError(RuntimeError):
    """Raised when a run cannot be proven to have reached real portal results.

    Covers a verification page, an unrecognized page (markup
    change / block variant), and a first page that never loaded.

    Must never be downgraded to `ran_out`: that feeds observation.complete, which
    gates the lifecycle sweep into writing terminal closures (see lifecycle.py).
    """


async def _raise_if_blocked(page: Page, where: str, status: int | None) -> None:
    """Classify a page that produced zero cards; return only if it is genuinely empty.

    Called exclusively from zero-card branches, so a healthy pass never pays for
    the page.content() read.
    """
    # The 10s selector timeout also fires on a merely slow page; settle first so a
    # half-rendered DOM is not mistaken for a block.
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    # A consent overlay suppresses rendering and would look identical. Idempotent.
    await dismiss_cookie_consent(page)

    html = await page.content()
    title = await page.title()
    lowered = html.lower()
    context = f"HTTP {status}, {len(html)} bytes, title: {title!r}"

    if BLOCK_MARKER in lowered or BLOCK_TITLE_MARKER in title.lower():
        raise BlockedError(
            f"BLOCKED: the portal served a verification page on {where} ({context})"
        )

    if status in BLOCK_STATUSES:
        raise BlockedError(f"BLOCKED: the portal refused {where} with HTTP {status} ({context})")

    if RESULTS_MARKER not in lowered:
        raise BlockedError(
            f"UNRECOGNIZED: no results chrome on {where} — a portal markup change or "
            f"a block variant, not a confirmed block ({context})"
        )


# JavaScript to extract listing cards from the search results page.
# Takes site_profile.CARD_JS_ARGS; returns a list of raw card objects.
EXTRACT_CARDS_JS = """
(p) => {
    const priceRe = new RegExp(p.pricePattern);
    const statusRe = new RegExp(p.statusPattern, 'i');
    const postalRe = new RegExp(p.postalCityPattern);
    const cards = [];
    const links = document.querySelectorAll(p.linkSelector);
    const seen = new Set();

    for (const link of links) {
        const href = link.getAttribute('href');
        if (!href || seen.has(href)) continue;
        seen.add(href);

        // The card container is typically the link's parent or grandparent
        const card = link.closest(p.cardSelector)
            || link.closest('li')
            || link.parentElement?.parentElement;
        if (!card) continue;

        // Street name is usually in the h2 or the first strong/bold text
        const streetEl = card.querySelector('h2')
            || link.querySelector('h2')
            || link;
        const street = streetEl?.textContent?.trim() || '';

        // Postal code and city are in a nearby element
        const allText = card.textContent || '';

        // Price
        const priceMatch = allText.match(priceRe);
        const priceText = priceMatch ? priceMatch[0] : '';

        // Status badge: heuristic text scan (robust to DOM changes). On a
        // normal available card these phrases don't appear; when the portal surfaces
        // a sold / under-offer state in results we capture it here.
        const statusMatch = allText.match(statusRe);
        const statusBadge = statusMatch ? statusMatch[0] : '';

        // Postal code + city, in the portal's postal format
        const postalMatch = allText.match(postalRe);
        const postalCity = postalMatch ? postalMatch[0].trim() : '';

        // Specs: area, bedrooms, rating from list items or spec elements
        const specEls = card.querySelectorAll(p.specSelector);
        const specs = Array.from(specEls).map(el => el.textContent?.trim() || '');

        cards.push({
            propertyType: card.getAttribute(p.propertyTypeAttr) || '',
            href: href.startsWith('http') ? href : new URL(href, location.origin).href,
            street,
            postalCity,
            priceText,
            statusBadge,
            specs,
            fullText: allText.substring(0, 500),
        });
    }
    return cards;
}
"""


def _parse_card(raw: dict) -> ListingSummary | None:
    """Parse a raw JS-extracted card dict into a ListingSummary."""
    url = raw.get("href", "")
    if not url:
        return None

    listing_id = extract_listing_id(url)
    if not listing_id:
        return None

    property_type = raw.get("propertyType", "").strip()

    street = raw.get("street", "").strip()
    postal_code, city = parse_postal_code(raw.get("postalCity", ""))

    price_text = raw.get("priceText", "")
    price = parse_price(price_text)

    # Parse specs (area, bedrooms, rating)
    specs = raw.get("specs", [])
    full_text = raw.get("fullText", "")
    living_area: int | None = None
    plot_area: int | None = None
    bedrooms: int | None = None
    rating = ""

    for spec in specs:
        spec_lower = spec.lower()
        if "m²" in spec or "m2" in spec:
            val = parse_area(spec)
            if val:
                if living_area is None:
                    living_area = val
                else:
                    plot_area = val
        elif any(k in spec_lower for k in P.ROOM_KEYWORDS):
            bedrooms = parse_bedrooms(spec)
        elif len(spec.strip()) <= 4 and spec.strip().rstrip("+").isalpha():
            rating = spec.strip()

    # Fallback: try to parse area/bedrooms from full text
    if living_area is None:
        area_match = _find_in_text(full_text, r"(\d+)\s*m²")
        if area_match:
            living_area = int(area_match)
    if bedrooms is None:
        bed_match = _find_in_text(full_text, P.ROOMS_TEXT_RE)
        if bed_match:
            bedrooms = int(bed_match)
    if not rating:
        rating_match = _find_in_text(full_text, P.RATING_RE)
        if rating_match:
            rating = rating_match

    return ListingSummary(
        listing_id=listing_id,
        url=url,
        property_type=property_type,
        street=street,
        postal_code=postal_code,
        city=city,
        price_text=price_text,
        price=price,
        living_area_m2=living_area,
        plot_area_m2=plot_area,
        bedrooms=bedrooms,
        rating=rating,
    )


def _find_in_text(text: str, pattern: str) -> str | None:
    """Find a regex group(1) in text, return None if not found."""
    import re

    match = re.search(pattern, text)
    return match.group(1) if match else None


def _normalize_city(name: str) -> str:
    """Comparable form of a city: slug 'example-city' == card text 'Example City'."""
    return " ".join(name.replace("-", " ").lower().split())


def _extract_valid_cities(areas: list[str]) -> set[str]:
    """Extract unique city names from area paths.

    Handles both 'city/neighborhood' format (e.g., 'example-city/centre')
    and bare city names (e.g., 'example-city', 'other-town').

    Returns normalized city names (see _normalize_city) for matching.
    """
    cities = set()
    for area in areas:
        if "/" in area:
            city = _normalize_city(area.split("/")[0])
        else:
            city = _normalize_city(area)
        cities.add(city)
    return cities


async def scrape_search_results(
    page: Page,
    settings: Settings,
    known_ids: set[str],
    observation: SweepObservation | None = None,
) -> list[ListingSummary]:
    """Scrape search results pages, returning new listings not in known_ids.

    Stops early after `consecutive_known_threshold` consecutive known IDs.
    Filters out listings from cities not in the configured areas.

    If `observation` is provided, records every valid (city-passing) listing_id
    seen — known and new alike — plus any status badge, so the lifecycle sweep
    can detect listings that have left the market. `observation.complete` is set
    True only when the pass traverses the full inventory (it runs out of
    results), never when it early-stops or hits the max_pages ceiling mid-run.
    """
    all_new: list[ListingSummary] = []
    consecutive_known = 0
    valid_cities = _extract_valid_cities(settings.areas)
    logger.info(f"Target cities: {sorted(valid_cities)}")

    ran_out = False
    total_seen = 0

    for page_num in range(1, settings.max_pages + 1):
        url = settings.search_url_page(page_num)
        logger.info(f"Scraping search page {page_num}: {url}")

        response = await page.goto(url, wait_until="domcontentloaded")
        status = response.status if response else None
        # Wait for listings to render
        try:
            await page.wait_for_selector(
                P.LISTING_LINK_SELECTOR, timeout=10000
            )
        except Exception:
            # Raises unless this is provably a real, empty results page.
            await _raise_if_blocked(page, f"search page {page_num}", status)
            logger.warning(f"No listings found on page {page_num}, stopping")
            ran_out = True
            break

        raw_cards = await page.evaluate(EXTRACT_CARDS_JS, P.CARD_JS_ARGS)
        logger.info(f"  Found {len(raw_cards)} cards on page {page_num}")

        if not raw_cards:
            await _raise_if_blocked(page, f"search page {page_num}", status)
            logger.info("  No cards extracted, stopping pagination")
            ran_out = True
            break

        page_new_count = 0
        page_filtered_count = 0
        for raw in raw_cards:
            listing = _parse_card(raw)
            if listing is None:
                continue

            # Filter out listings from cities not in configured areas
            if listing.city:
                if _normalize_city(listing.city) not in valid_cities:
                    logger.warning(
                        f"  Filtered out {listing.listing_id} ({listing.street}): "
                        f"city '{listing.city}' not in configured areas"
                    )
                    page_filtered_count += 1
                    continue
            else:
                # No city extracted - log and skip
                logger.warning(
                    f"  Filtered out {listing.listing_id} ({listing.street}): "
                    f"no city extracted from '{raw.get('postalCity', '')}'"
                )
                page_filtered_count += 1
                continue

            # Record presence (known + new) for lifecycle reconciliation.
            if observation is not None:
                observation.seen_active_ids.add(listing.listing_id)
                badge = raw.get("statusBadge", "")
                if badge:
                    observation.badge_by_id[listing.listing_id] = badge
            total_seen += 1

            if listing.listing_id in known_ids:
                consecutive_known += 1
                if consecutive_known >= settings.consecutive_known_threshold:
                    logger.info(
                        f"  Hit {consecutive_known} consecutive known IDs, "
                        "stopping early"
                    )
                    return all_new
            else:
                consecutive_known = 0
                all_new.append(listing)
                known_ids.add(listing.listing_id)
                page_new_count += 1

        known_count = len(raw_cards) - page_new_count - page_filtered_count
        logger.info(
            f"  Page {page_num}: {page_new_count} new, "
            f"{known_count} known, {page_filtered_count} filtered"
        )

        # Delay between pages
        if page_num < settings.max_pages:
            await jittered_delay(
                settings.page_delay_seconds, settings.delay_jitter_factor
            )

    # A pass is only "complete" if it ran out of results (reached the end of the
    # inventory) AND actually saw listings — a transient page-1 failure must not
    # be read as "everything disappeared".
    if observation is not None:
        observation.complete = ran_out and total_seen > 0

    return all_new


async def scrape_sold_ids(page: Page, settings: Settings) -> dict[str, str]:
    """Enumerate sold / under-offer listings from the portal's sold view.

    Paginates the status-filtered search (`search_url_sold_page`) up to
    `sold_sweep_max_pages` and returns {listing_id: normalized_status} for the
    configured cities. This is the precise sell signal: an id here that we still
    track as open is a confirmed close. Every listing in this view is sold or
    under offer, so an ambiguous/empty card badge defaults to "Sold". Best-effort: a page error
    stops pagination and returns whatever was gathered.
    """
    sold: dict[str, str] = {}
    valid_cities = _extract_valid_cities(settings.areas)

    for page_num in range(1, settings.sold_sweep_max_pages + 1):
        url = settings.search_url_sold_page(page_num)
        logger.info(f"Scraping sold view page {page_num}: {url}")
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else None
            try:
                await page.wait_for_selector(P.LISTING_LINK_SELECTOR, timeout=10000)
            except Exception:
                await _raise_if_blocked(page, f"sold view page {page_num}", status)
                logger.info("  No sold listings on this page, stopping")
                break
            raw_cards = await page.evaluate(EXTRACT_CARDS_JS, P.CARD_JS_ARGS)
        except BlockedError:
            # A blocked sold view must not return {} — that silently rewrites
            # genuine sales as "Closed-Unknown" in the lifecycle sweep.
            raise
        except Exception as e:
            logger.warning(f"  Sold view page {page_num} failed: {e}")
            break

        if not raw_cards:
            break

        page_count = 0
        for raw in raw_cards:
            listing = _parse_card(raw)
            if listing is None or not listing.listing_id:
                continue
            if listing.city and _normalize_city(listing.city) not in valid_cities:
                continue
            status = normalize_status(raw.get("statusBadge", "")) or STATUS_SOLD
            sold[listing.listing_id] = status
            page_count += 1

        logger.info(f"  Sold view page {page_num}: {page_count} sold/under-offer listings")

        if page_num < settings.sold_sweep_max_pages:
            await jittered_delay(settings.page_delay_seconds, settings.delay_jitter_factor)

    logger.info(f"Sold sweep collected {len(sold)} sold/under-offer ids")
    return sold
