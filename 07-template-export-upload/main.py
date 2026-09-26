"""Template export bot: download every document template and list them in Excel.

Signs in to the portal through a reused SSO browser session, opens the
document template grid, filters it to active templates, and for
each row captures the detail URL, downloads the template file, and records
everything in an Excel file.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for shared/
from shared.browser_session import (  # noqa: E402
    cleanup_profile,
    copy_browser_profile,
    launch_browser,
    safe_goto,
    sso_login,
)

from export_excel import export_to_excel  # noqa: E402

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"

logger = logging.getLogger(__name__)

# Selectors and UI labels are placeholders; adapt them to the target portal.
SELECTORS = {
    # Menu clicks from the app home page to the template grid: (role, name, nth or None)
    "menu_path": [
        ("link", "Admin", None),
        ("button", "Manage", None),
        ("link", "Settings", None),
        ("link", "Templates", 3),  # 4th link with this name (exact match)
    ],
    "grid_filter_value": "True",  # value typed into the last column's floating filter
    "download_button": re.compile(r"Download$"),  # button text may carry an icon-glyph prefix
    "back_link_text": "All templates",
}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(SCRIPT_DIR / "template_export.log", encoding="utf-8"),
        ],
    )


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def navigate_to_document_grid(page, app_url: str) -> None:
    """Navigate from the app home page through the menus to the filtered template grid."""
    logger.info("Navigating to document template grid...")

    await safe_goto(page, app_url)

    # Menu path taken from a Playwright codegen recording. An nth index picks
    # one of several links with the same name.
    for role, name, nth in SELECTORS["menu_path"]:
        locator = page.get_by_role(role, name=name, exact=nth is not None)
        await (locator.nth(nth) if nth is not None else locator).click()
        await page.wait_for_load_state("domcontentloaded")

    # Keep only active templates via the last column's filter
    await apply_grid_filter(page, SELECTORS["grid_filter_value"])

    logger.info("Document grid loaded and filtered")


async def apply_grid_filter(page, filter_value: str) -> None:
    """Open the AG-Grid floating filter on the last column and enter a filter value."""
    filter_button = page.locator(
        ".ag-header-cell.ag-floating-filter.ag-column-last > .ag-floating-filter-button > .ag-button"
    )
    await filter_button.click()

    # The filter popup contains two condition inputs; target the first one
    filter_input = page.locator(".ag-popup .ag-filter").get_by_role("textbox", name="Filter Value").first
    await filter_input.fill(filter_value)
    await filter_input.press("Enter")
    await page.wait_for_timeout(1000)


async def extract_grid_headers(page) -> list[dict]:
    """Extract column headers from the AG-Grid header row.

    Returns list of dicts in DOM order: [{"col_id": "type", "label": "Type"}, ...]
    """
    headers = await page.evaluate("""
        () => {
            const headerCells = document.querySelectorAll(
                '.ag-header-row:not(.ag-header-row-floating-filter) .ag-header-cell'
            );
            return [...headerCells].map(cell => ({
                col_id: cell.getAttribute('col-id') || '',
                label: (cell.querySelector('.ag-header-cell-text') || {}).innerText || ''
            }));
        }
    """)
    logger.debug(f"Grid headers: {headers}")
    return headers


async def get_all_grid_rows(page) -> list[dict]:
    """Extract all filtered row data via AG-Grid JavaScript API, bypassing virtualization."""
    # Debug: check what AG-Grid API access methods are available
    api_debug = await page.evaluate("""
        () => {
            const gridEl = document.querySelector('.ag-root-wrapper');
            if (!gridEl) return { found: false };
            const keys = Object.keys(gridEl).filter(k => k.startsWith('__') || k.includes('grid') || k.includes('ag'));
            return {
                found: true,
                keys: keys.slice(0, 20),
                hasAgGridInstance: !!gridEl.__agGridInstance,
                hasGridOptions: !!gridEl.gridOptions,
                windowAgGrid: !!window.agGrid,
            };
        }
    """)
    logger.debug(f"AG-Grid API probe: {api_debug}")

    row_data = await page.evaluate("""
        () => {
            const gridEl = document.querySelector('.ag-root-wrapper');
            if (!gridEl) return null;

            // Probe the places an app may expose the grid API; which one works
            // depends on the AG-Grid version and how the app wires it up.
            const api = gridEl.__agGridInstance?.api
                     || gridEl.gridOptions?.api
                     || window.agGrid?.gridOptions?.api;

            if (api) {
                const rows = [];
                api.forEachNodeAfterFilterAndSort(node => {
                    if (node.data) {
                        const cells = {};
                        for (const [k, v] of Object.entries(node.data)) {
                            cells[k] = String(v ?? '');
                        }
                        rows.push({
                            rowIndex: node.rowIndex,
                            cells: cells,
                            dataKeys: Object.keys(node.data)
                        });
                    }
                });
                return rows;
            }

            return null;
        }
    """)

    if row_data:
        logger.debug(f"AG-Grid API returned {len(row_data)} rows")
        logger.debug(f"First row data keys: {row_data[0].get('dataKeys', [])}")
        logger.debug(f"First row cells: {row_data[0].get('cells', {})}")
    else:
        logger.debug("AG-Grid API returned null - API not accessible")

    return row_data


async def collect_rows_from_dom(page, headers: list[dict]) -> list[dict]:
    """Fallback: scroll through the grid to collect all row data from the DOM."""
    row_data = []
    seen_indices = set()

    # Debug: dump the grid DOM structure to understand the layout
    grid_structure = await page.evaluate("""
        () => {
            const vp = document.querySelector('.ag-body-viewport');
            if (!vp) return { found: false };
            return {
                found: true,
                scrollHeight: vp.scrollHeight,
                clientHeight: vp.clientHeight,
                childContainers: [...vp.querySelectorAll('[class*="ag-"]')]
                    .slice(0, 15)
                    .map(el => ({
                        tag: el.tagName,
                        class: el.className.split(' ').filter(c => c.startsWith('ag-')).join(' '),
                        childCount: el.children.length,
                    })),
                totalRowsInDOM: vp.querySelectorAll('.ag-row').length,
                sampleRowAttrs: (() => {
                    const row = vp.querySelector('.ag-row');
                    if (!row) return null;
                    return {
                        outerHTML: row.outerHTML.slice(0, 500),
                        rowIndex: row.getAttribute('row-index'),
                        rowId: row.getAttribute('row-id'),
                        cellCount: row.querySelectorAll('.ag-cell').length,
                        cellTexts: [...row.querySelectorAll('.ag-cell')]
                            .map(c => c.innerText.trim().slice(0, 60)),
                    };
                })(),
            };
        }
    """)
    logger.debug(f"Grid DOM structure: {grid_structure}")

    # Also check if rows exist outside ag-body-viewport (e.g., in ag-body)
    alt_row_count = await page.evaluate("""
        () => {
            const selectors = [
                '.ag-body-viewport .ag-row',
                '.ag-root .ag-row',
                '.ag-body .ag-row',
                '[role="row"]',
                '.ag-row',
            ];
            return selectors.map(s => ({ selector: s, count: document.querySelectorAll(s).length }));
        }
    """)
    logger.debug(f"Row counts by selector: {alt_row_count}")

    # Scroll incrementally to materialize all virtualized rows
    scroll_height = await page.evaluate("document.querySelector('.ag-body-viewport').scrollHeight")
    viewport_height = await page.evaluate("document.querySelector('.ag-body-viewport').clientHeight")
    logger.debug(f"Viewport: scrollHeight={scroll_height}, clientHeight={viewport_height}")

    scroll_pos = 0
    while scroll_pos <= scroll_height:
        await page.evaluate(f"document.querySelector('.ag-body-viewport').scrollTop = {scroll_pos}")
        await page.wait_for_timeout(300)

        rows = page.locator(".ag-body-viewport .ag-row")
        count = await rows.count()
        for i in range(count):
            row = rows.nth(i)
            idx = await row.get_attribute("row-index")
            if idx and idx not in seen_indices:
                seen_indices.add(idx)
                cells_locator = row.locator(".ag-cell")
                cell_count = await cells_locator.count()
                cell_data = {}
                for ci in range(cell_count):
                    cell = cells_locator.nth(ci)
                    col_id = await cell.get_attribute("col-id") or f"col-{ci}"
                    cell_text = (await cell.inner_text()).strip()
                    cell_data[col_id] = cell_text
                row_data.append({"rowIndex": int(idx), "cells": cell_data})
                logger.debug(f"  DOM row-index={idx}: cells={cell_data}")

        logger.debug(f"Scroll pos {scroll_pos}: {count} rows in DOM, {len(seen_indices)} unique collected")
        scroll_pos += viewport_height

    row_data.sort(key=lambda r: r["rowIndex"])
    logger.debug(f"Total rows collected from DOM: {len(row_data)}")
    return row_data


async def scrape_document_urls(page, docs_dir: Path) -> tuple[list[dict], list[dict]]:
    """Iterate all AG-Grid rows, click each to capture document name and URL.

    Downloads each document template to *docs_dir* and records the local
    file path in the ``_file_path`` key of each document dict.

    Returns (headers, documents) where headers is a list of {"col_id", "label"}
    and documents is a list of dicts with col_id keys plus "_url" and "_file_path".
    """
    documents = []

    # Extract column headers from the grid
    headers = await extract_grid_headers(page)

    # Try the AG-Grid JS API first (bypasses virtualization entirely)
    row_data = await get_all_grid_rows(page)
    if not row_data:
        logger.info("AG-Grid API not available, falling back to DOM scroll...")
        row_data = await collect_rows_from_dom(page, headers)

    total = len(row_data)
    logger.info(f"Found {total} rows in filtered grid")

    if total == 0:
        logger.warning("No rows found in grid after filtering")
        return headers, documents

    # Derive a display name from cells for logging (use 3rd header column if available)
    def _display_name(rd):
        cells = rd.get("cells", {})
        if len(headers) > 2:
            return cells.get(headers[2]["col_id"], "")
        vals = list(cells.values())
        return vals[0] if vals else "?"

    logger.info(f"Row names: {[_display_name(r) for r in row_data[:5]]}...")

    # Click each row by row-index, capture URL, navigate back
    for i, rd in enumerate(row_data):
        row_index = rd["rowIndex"]
        doc_name = _display_name(rd)
        logger.info(f"[{i + 1}/{total}] Processing: {doc_name} (row-index={row_index})")

        # Scroll the row into view and click its first cell
        await page.evaluate(f"document.querySelector('.ag-body-viewport').scrollTop = {row_index * 42}")
        await page.wait_for_timeout(300)

        # Debug: check the row is actually in the DOM after scrolling
        row_selector = f'.ag-body-viewport .ag-row[row-index="{row_index}"]'
        row_exists = await page.locator(row_selector).count()
        logger.debug(f"  Row selector '{row_selector}' found: {row_exists} elements")

        if row_exists == 0:
            # Debug: dump what rows ARE visible right now
            visible_indices = await page.evaluate("""
                () => [...document.querySelectorAll('.ag-body-viewport .ag-row')]
                    .map(r => ({ idx: r.getAttribute('row-index'), text: r.innerText.trim().slice(0, 80) }))
            """)
            logger.debug(f"  Visible rows after scroll: {visible_indices}")
            logger.warning(f"  Row {row_index} not found in DOM - skipping")
            continue

        row = page.locator(row_selector).first
        await row.scroll_into_view_if_needed()

        # Debug: what cells does this row have?
        cell_count = await row.locator(".ag-cell").count()
        if cell_count > 0:
            first_cell_text = (await row.locator(".ag-cell").first.inner_text()).strip()
            logger.debug(f"  Row has {cell_count} cells, first cell text: '{first_cell_text[:60]}'")
        else:
            logger.debug("  Row has 0 .ag-cell elements")
            # Debug: dump the row's inner HTML
            row_html = await row.evaluate("el => el.innerHTML.slice(0, 500)")
            logger.debug(f"  Row innerHTML: {row_html}")

        await row.locator(".ag-cell").first.click()

        # Wait for the detail page to load
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1000)

        # Debug: log current URL and page title
        logger.debug(f"  Page URL after click: {page.url}")
        logger.debug(f"  Page title: {await page.title()}")

        # Capture the document URL from the detail page
        doc_url = page.url
        doc_record = dict(rd.get("cells", {}))
        doc_record["_url"] = doc_url

        # Download the document template
        file_path = ""
        try:
            async with page.expect_download(timeout=15000) as download_info:
                await page.get_by_role("button", name=SELECTORS["download_button"]).click()
            download = await download_info.value
            suggested = download.suggested_filename or f"document_{row_index}.bin"
            save_path = docs_dir / suggested
            # Avoid overwriting: find a free filename
            counter = 1
            while save_path.exists():
                save_path = docs_dir / f"{save_path.stem}_{counter}{save_path.suffix}"
                counter += 1
            await download.save_as(str(save_path))
            file_path = str(save_path)
            logger.info(f"  Downloaded: {file_path}")
        except Exception as e:
            logger.warning(f"  Download failed for row {row_index}: {e}")

        doc_record["_file_path"] = file_path
        documents.append(doc_record)
        logger.info(f"  URL: {doc_url}")

        # Navigate back to the grid via the "All templates" link
        back_link = page.locator("a").filter(has_text=SELECTORS["back_link_text"])
        back_count = await back_link.count()
        logger.debug(f"  'All templates' links found: {back_count}")
        if back_count == 0:
            # Debug: dump all links on the page
            all_links = await page.evaluate("""
                () => [...document.querySelectorAll('a')]
                    .map(a => ({ text: a.innerText.trim().slice(0, 60), href: a.href }))
                    .filter(a => a.text)
                    .slice(0, 20)
            """)
            logger.debug(f"  Available links on page: {all_links}")
            logger.warning("  'All templates' link not found - using browser back")
            await page.go_back(wait_until="domcontentloaded")
        else:
            await back_link.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1000)

        # Verify the filter is still applied; re-apply if needed
        current_rows = await get_all_grid_rows(page)
        if current_rows is None:
            current_count = await page.locator(".ag-body-viewport .ag-row").count()
        else:
            current_count = len(current_rows)
        logger.debug(f"  After return: {current_count} rows (expected {total})")
        if current_count != total:
            logger.info("Filter was reset after navigation - re-applying...")
            await apply_grid_filter(page, SELECTORS["grid_filter_value"])

    return headers, documents


async def run() -> None:
    config = load_config()
    portal = config["portal"]
    browser_cfg = config["browser"]
    output_cfg = config["output"]
    temp_dir = None

    try:
        # Phase 1: Copy the signed-in browser profile
        temp_dir = copy_browser_profile(
            browser_cfg["user_data_dir"] or None, browser_cfg["profile_name"], browser_cfg["channel"]
        )

        # Phase 2: Launch browser
        pw, context, page = await launch_browser(
            temp_dir, browser_cfg["headless"], browser_cfg["channel"], browser_cfg["timeout_ms"]
        )

        # Phase 3: SSO login
        await sso_login(
            page,
            os.environ.get("PORTAL_BASE_URL", portal["entry_url"]),
            portal["sso_button_name"],
            portal["account_name"],
            portal["app_url"],
        )

        # Phase 4: Navigate to document grid
        await navigate_to_document_grid(page, portal["app_url"])

        # Phase 5: Scrape all document URLs and download templates
        docs_dir = SCRIPT_DIR / "docs"
        docs_dir.mkdir(exist_ok=True)
        headers, documents = await scrape_document_urls(page, docs_dir)

        # Phase 6: Export to Excel
        output_file = str(SCRIPT_DIR / output_cfg["filename"])
        export_to_excel(documents, output_file, output_cfg["sheet_name"], headers)

        logger.info("\n=== COMPLETED ===")
        logger.info(f"Documents exported: {len(documents)}")
        logger.info(f"Output file: {output_file}")

        await context.close()
        await pw.stop()

    finally:
        # Phase 7: Cleanup
        cleanup_profile(temp_dir)


async def main() -> None:
    setup_logging()
    try:
        await run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Automation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
