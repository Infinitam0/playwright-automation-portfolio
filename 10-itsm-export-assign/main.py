"""Export bot: list every person unassigned from an asset, from its event history, to Excel."""

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for shared/
from shared.browser_session import cleanup_profile, copy_browser_profile, launch_browser  # noqa: E402

from models import AppConfig  # noqa: E402
from navigate_assets import navigate_to_asset_overview, open_asset_detail, open_history  # noqa: E402
from portal_login import ensure_app_loaded, login  # noqa: E402
from scrape_export import export_to_excel, load_all_events, scrape_events  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path: str | None = None) -> AppConfig:
    # Default to config.yaml next to this script file, not the process CWD.
    if path is None:
        path = str(Path(__file__).parent / "config.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig(**raw)


async def main():
    config = load_config()
    temp_dir = None

    try:
        # Phase 1: Setup
        logger.info("=== Phase 1: Setup ===")
        temp_dir = copy_browser_profile(
            config.browser.user_data_dir or None, config.browser.profile_name, config.browser.channel
        )

        # Phase 2: Launch & Login
        logger.info("=== Phase 2: Launch & Login ===")
        pw, context, page = await launch_browser(temp_dir, config.browser.headless, config.browser.channel)
        await login(page, config)
        await ensure_app_loaded(page)

        # Phase 3: Navigate to the asset's event history
        logger.info("=== Phase 3: Navigate ===")
        await navigate_to_asset_overview(page)
        detail_frame = await open_asset_detail(page, config)
        events_frame = await open_history(detail_frame)

        # Phase 4: Load all events
        logger.info("=== Phase 4: Load All Events ===")
        await load_all_events(events_frame, page, config)

        # Phase 5: Scrape & Filter
        logger.info("=== Phase 5: Scrape & Filter ===")
        filtered_events = await scrape_events(events_frame, config)

        # Phase 6: Export
        logger.info("=== Phase 6: Export ===")
        output_path = os.path.join(os.path.dirname(__file__), config.output_file)
        export_to_excel(filtered_events, output_path)

        await context.close()
        await pw.stop()

    finally:
        cleanup_profile(temp_dir)


if __name__ == "__main__":
    asyncio.run(main())
