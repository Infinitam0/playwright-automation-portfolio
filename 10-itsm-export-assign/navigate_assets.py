"""Menu and iframe navigation to one asset's detail page (used by both bots)."""

import logging

from playwright.async_api import Frame

logger = logging.getLogger(__name__)

# Selectors are placeholders; adapt them to the target portal.
SELECTORS = {
    "menu_path": ("Menu", "Assets"),  # left-hand menu labels, clicked in order
    "toolbar_frame_url": "assets/toolbar",  # URL fragment of the iframe holding the asset buttons
    "overview_frame_url": "assets/overview",  # URL fragment of the asset list iframe
    "detail_frame_url": "assets/",  # URL fragment shared by asset detail iframes
    "overview_button": "[data-testid='open-asset-list']",
    "asset_type_option": "[data-testid='asset-type-{id}'] div",  # {id} = portal.asset_type_id
    "asset_link": "[data-testid='asset-link-{id}']",  # {id} = portal.asset_id
    "history_button": "History",  # opens the asset's event history
    "event_row": ".history-item",
}


def find_frame_by_url(page, url_substring: str):
    """Find a Playwright Frame whose URL contains the given substring."""
    for frame in page.frames:
        if url_substring in frame.url:
            return frame
    return None


async def _wait_for_frame_by_url(page, url_substring: str, timeout_ms: int = 30000) -> Frame:
    """Poll for a frame whose URL contains *url_substring*. Returns the Frame."""
    polls = timeout_ms // 500
    for _ in range(polls):
        frame = find_frame_by_url(page, url_substring)
        if frame:
            return frame
        await page.wait_for_timeout(500)
    raise RuntimeError(f"Timed out waiting for frame with URL containing '{url_substring}'")


async def _click_menu_div(page, text: str, timeout: int = 15000) -> None:
    """Click a visible div with exact text in the portal's left-hand menu.

    The SPA shell renders menu items as absolutely-positioned divs on the
    main page (not inside iframes). Element IDs are dynamic per session, so we
    match by visible text content and pick the smallest element by area to avoid
    clicking a large container.
    """
    # Wait for at least one matching element to appear
    locator = page.locator(f"div:text-is('{text}'):visible")
    await locator.first.wait_for(state="visible", timeout=timeout)

    count = await locator.count()
    best = None
    best_area = float("inf")
    for i in range(count):
        el = locator.nth(i)
        box = await el.bounding_box()
        if box:
            area = box["width"] * box["height"]
            if area < best_area:
                best_area = area
                best = el

    if best is None:
        raise RuntimeError(f"No visible div with text '{text}' found on the page.")

    logger.info(f"Clicking menu div '{text}' (area={best_area:.0f}px²)")
    await best.click()


async def navigate_to_asset_overview(page) -> None:
    """Navigate through the portal menu to the asset overview panel."""
    # Steps 1-2: Click through the left-hand menu (top level, then sub-menu)
    for label in SELECTORS["menu_path"]:
        logger.info(f"Clicking '{label}' menu item...")
        await _click_menu_div(page, label)

    # Step 3: Find the frame containing the asset buttons and click the overview button
    toolbar_url = SELECTORS["toolbar_frame_url"]
    logger.info("Looking for asset toolbar frame...")
    for _ in range(30):  # poll for up to 15s
        frame = find_frame_by_url(page, toolbar_url)
        if frame:
            break
        await page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Could not find frame with URL containing '{toolbar_url}'")

    asset_overview_btn = frame.locator(SELECTORS["overview_button"])
    await asset_overview_btn.wait_for(state="visible", timeout=30000)
    logger.info("Clicking asset overview button...")
    await asset_overview_btn.click()


async def open_asset_detail(page, config) -> Frame:
    """Select the asset type, search for the asset, open it, and return its detail frame."""
    portal = config.portal

    # Find the overview frame (asset type selector + asset search)
    logger.info("Waiting for asset overview frame...")
    overview_frame = await _wait_for_frame_by_url(page, SELECTORS["overview_frame_url"])

    # Select asset type
    asset_type_btn = overview_frame.locator(SELECTORS["asset_type_option"].format(id=portal.asset_type_id)).first
    await asset_type_btn.wait_for(state="visible", timeout=30000)
    logger.info("Clicking asset type option...")
    await asset_type_btn.click()

    # Search for asset (UI label on the target site)
    search_box = overview_frame.get_by_role("textbox", name="Search assets")
    await search_box.wait_for(state="visible", timeout=15000)
    logger.info(f"Filling search box with '{portal.asset_search_term}'...")
    await search_box.click()
    await search_box.fill(portal.asset_search_term)

    # Click asset link
    asset_link = overview_frame.locator(SELECTORS["asset_link"].format(id=portal.asset_id))
    await asset_link.wait_for(state="visible", timeout=15000)
    logger.info("Clicking asset link...")

    # Remember current frame URLs so we can detect the new detail frame
    known_urls = {f.url for f in page.frames}
    await asset_link.click()

    # Wait for a new frame to appear (the asset detail page)
    logger.info("Waiting for asset detail frame to appear...")
    detail_frame = None
    for _ in range(60):  # poll up to 30s
        for frame in page.frames:
            if frame.url not in known_urls and SELECTORS["detail_frame_url"] in frame.url:
                detail_frame = frame
                break
        if detail_frame:
            break
        await page.wait_for_timeout(500)

    if detail_frame is None:
        raise RuntimeError("Could not find the asset detail frame after clicking the asset link.")

    logger.info(f"Found asset detail frame: {detail_frame.url}")
    return detail_frame


async def open_history(detail_frame: Frame) -> Frame:
    """Open the asset's event history and wait until events are listed."""
    label = SELECTORS["history_button"]
    logger.info(f"Waiting for '{label}' button...")
    history_btn = detail_frame.get_by_role("button", name=label)
    await history_btn.wait_for(state="visible", timeout=30000)
    logger.info(f"Clicking '{label}'...")
    await history_btn.click()

    # Wait for events to populate
    logger.info("Waiting for events list to be populated...")
    await detail_frame.locator(SELECTORS["event_row"]).first.wait_for(state="attached", timeout=30000)

    logger.info("Opened event history successfully.")
    return detail_frame
