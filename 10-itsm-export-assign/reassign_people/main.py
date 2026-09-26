"""Companion bot: re-assign the people listed in the export Excel to the asset."""

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

# The export bot's folder (models, login, navigation) and the repo root (shared/)
sys.path[1:1] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
from assign_person import assign_all_persons, wait_for_assign_menu  # noqa: E402
from read_excel import read_person_names  # noqa: E402
from shared.browser_session import cleanup_profile, copy_browser_profile, launch_browser  # noqa: E402

from models import AssignConfig  # noqa: E402
from navigate_assets import navigate_to_asset_overview, open_asset_detail  # noqa: E402
from portal_login import ensure_app_loaded, login  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path: str | None = None) -> AssignConfig:
    # Default to config.yaml next to this script file, not the process CWD.
    if path is None:
        path = str(Path(__file__).parent / "config.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AssignConfig(**raw)


async def main():
    config = load_config()
    temp_dir = None

    try:
        # Phase 1: Read person names from Excel
        logger.info("=== Phase 1: Read Input ===")
        input_path = os.path.join(os.path.dirname(__file__), config.input_file)
        person_names = read_person_names(input_path)
        if not person_names:
            logger.warning("No person names found in input file. Nothing to do.")
            return
        logger.info(f"Will assign {len(person_names)} persons.")

        # Phase 2: Copy the signed-in browser profile
        logger.info("=== Phase 2: Setup ===")
        temp_dir = copy_browser_profile(
            config.browser.user_data_dir or None, config.browser.profile_name, config.browser.channel
        )

        # Phase 3: Launch browser & login
        logger.info("=== Phase 3: Launch & Login ===")
        pw, context, page = await launch_browser(temp_dir, config.browser.headless, config.browser.channel)
        await login(page, config)
        await ensure_app_loaded(page)

        # Phase 4: Navigate to asset overview -> asset -> detail frame
        logger.info("=== Phase 4: Navigate ===")
        await navigate_to_asset_overview(page)
        detail_frame = await open_asset_detail(page, config)
        await wait_for_assign_menu(detail_frame)

        # Phase 5: Assign all persons from Excel
        logger.info("=== Phase 5: Assign Persons ===")
        await assign_all_persons(detail_frame, person_names, page, config.assignment_delay_ms)

        # Phase 6: Cleanup
        logger.info("=== Phase 6: Done ===")
        await context.close()
        await pw.stop()

    finally:
        cleanup_profile(temp_dir)


if __name__ == "__main__":
    asyncio.run(main())
