"""Detail page scraping: extract the features section (property attributes) via dt/dd pairs."""

from __future__ import annotations

import logging

from playwright.async_api import Page

from src.config import Settings
from src.models import ListingDetail
from src.scraper import site_profile as P
from src.scraper.timing import jittered_delay

logger = logging.getLogger(__name__)

# JavaScript to extract all dt/dd pairs from the detail page.
# Takes site_profile.DETAIL_JS_ARGS; returns {label: value}.
EXTRACT_FEATURES_JS = """
(p) => {
    const result = {};
    const dts = document.querySelectorAll('dt');
    for (const dt of dts) {
        const key = dt.textContent?.trim();
        const dd = dt.nextElementSibling;
        if (key && dd && dd.tagName === 'DD') {
            const value = dd.textContent?.trim();
            if (value) {
                result[key] = value;
            }
        }
    }

    // Try to extract neighbourhood from page header link
    const neighbourhoodLink = document.querySelector(p.neighbourhoodSelector);
    if (neighbourhoodLink) {
        const nbText = neighbourhoodLink.textContent?.trim();
        if (nbText) {
            result[p.neighbourhoodLabel] = nbText;
        }
    }

    // Also try to get the "date listed" value from the page text
    const allText = document.body?.textContent || '';
    const dateListedMatch = allText.match(new RegExp(p.dateListedPattern, 'i'));
    if (dateListedMatch) {
        result[p.dateListedLabel] = dateListedMatch[1].trim();
    }

    // Try to find price per m2 via regex fallback
    const priceM2Match = allText.match(new RegExp(p.pricePerM2Pattern, 'i'));
    if (priceM2Match) {
        result[p.pricePerM2Label] = priceM2Match[1] + '/m²';
    }

    return result;
}
"""


async def scrape_detail(page: Page, url: str, settings: Settings) -> ListingDetail:
    """Navigate to a detail page and extract the features-section fields."""
    logger.debug(f"Scraping detail: {url}")

    await page.goto(url, wait_until="domcontentloaded")

    # Wait for the features content to load
    try:
        await page.wait_for_selector("dt", timeout=8000)
    except Exception:
        logger.warning(f"No dt elements found on {url}")
        return ListingDetail()

    raw_fields: dict[str, str] = await page.evaluate(EXTRACT_FEATURES_JS, P.DETAIL_JS_ARGS)
    logger.debug(f"  Extracted {len(raw_fields)} fields from detail page")

    # Map the portal's field labels to our model
    detail_data: dict[str, str] = {}
    for label, value in raw_fields.items():
        field = P.FEATURE_LABELS.get(label)
        if field:
            detail_data[field] = value

    return ListingDetail(**detail_data)


async def scrape_details_batch(
    page: Page,
    urls: list[str],
    settings: Settings,
) -> dict[str, ListingDetail]:
    """Scrape detail pages for a batch of URLs.

    Returns a dict mapping URL -> ListingDetail.
    """
    results: dict[str, ListingDetail] = {}
    total = len(urls)

    for i, url in enumerate(urls, 1):
        logger.info(f"  Detail {i}/{total}: {url}")
        try:
            detail = await scrape_detail(page, url, settings)
            results[url] = detail
        except Exception as e:
            logger.error(f"  Failed to scrape detail {url}: {e}")
            results[url] = ListingDetail()

        if i < total:
            await jittered_delay(
                settings.detail_delay_seconds, settings.delay_jitter_factor
            )

    return results
