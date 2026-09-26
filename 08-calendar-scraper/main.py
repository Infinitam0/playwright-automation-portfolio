"""Calendar scraper: export one user's calendar appointments to Excel.

Signs in to the portal through a reused SSO browser session, opens a specific
user's calendar, walks it week by week from a configurable date, extracts
appointment details, and writes them incrementally to Excel.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for shared/
from shared.browser_session import (  # noqa: E402
    cleanup_profile,
    copy_browser_profile,
    launch_browser,
    sso_login,
)

from calendar_scraper import navigate_to_user_calendar, scrape_calendar  # noqa: E402
from export_excel import ExcelWriter  # noqa: E402

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(SCRIPT_DIR / "calendar_export.log", encoding="utf-8"),
        ],
    )


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run() -> None:
    config = load_config()
    portal = config["portal"]
    browser_cfg = config["browser"]
    user_code = config["user"]["search_code"]
    start_date = config["calendar"]["start_date"]
    end_date = config["calendar"]["end_date"]
    output_cfg = config["output"]
    temp_dir = None

    output_file = str(SCRIPT_DIR / output_cfg["filename"])
    excel_writer = ExcelWriter(output_file, output_cfg["sheet_name"])
    logger.info(f"Excel file: {output_file} ({excel_writer.row_count} existing rows)")

    # Determine the effective start date for calendar navigation
    nav_start_date = start_date
    if excel_writer.max_exported_date:
        nav_start_date = excel_writer.max_exported_date
        logger.info(f"Resuming: will navigate to {nav_start_date} instead of {start_date}")

    try:
        # Phase 1: Copy the signed-in browser profile
        temp_dir = copy_browser_profile(
            browser_cfg["user_data_dir"] or None, browser_cfg["profile_name"], browser_cfg["channel"]
        )

        # Phase 2: Launch browser
        pw, context, page = await launch_browser(
            temp_dir, browser_cfg["headless"], browser_cfg["channel"], browser_cfg["timeout_ms"]
        )

        # Phase 3: SSO login, then warm up the calendar domain
        await sso_login(
            page,
            os.environ.get("PORTAL_BASE_URL", portal["entry_url"]),
            portal["sso_button_name"],
            portal["account_name"],
            portal["calendar_url"],
        )

        # Phase 4: Navigate to the user's calendar at start/resume date
        await navigate_to_user_calendar(page, portal["calendar_url"], user_code, nav_start_date)

        # Phase 5: Scrape calendar appointments
        new_count = await scrape_calendar(page, start_date, end_date, excel_writer)

        logger.info("\n=== COMPLETED ===")
        logger.info(f"New appointments exported: {new_count}")
        logger.info(f"Total rows in Excel: {excel_writer.row_count}")
        logger.info(f"Output file: {output_file}")

        await context.close()
        await pw.stop()

    finally:
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
