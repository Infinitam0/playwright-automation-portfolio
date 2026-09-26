"""Template upload bot: replace document templates in bulk.

Signs in to the portal through a reused SSO browser session, reads document
URLs and local file paths from the export Excel, opens each document's detail
page, and uploads the replacement file through the portal's "Replace" flow.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for shared/
from import_excel import read_document_records  # noqa: E402
from shared.browser_session import (  # noqa: E402
    cleanup_profile,
    copy_browser_profile,
    launch_browser,
    safe_goto,
    sso_login,
)

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"

logger = logging.getLogger(__name__)

# Selectors and UI labels are placeholders; adapt them to the target portal.
SELECTORS = {
    "replace_button": re.compile(r"Replace$"),  # button text may carry an icon-glyph prefix
    "file_input_button": "Choose File",
}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(SCRIPT_DIR / "template_upload.log", encoding="utf-8"),
        ],
    )


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def upload_single_document(page, doc: dict, upload_cfg: dict) -> dict:
    """Upload a single document file via the Replace flow.

    Returns a result dict with keys: name, url, status, error.
    """
    name = doc["name"]
    url = doc["url"]
    file_path = doc["file_path"]

    # Verify the local file exists before navigating
    if not Path(file_path).is_file():
        return {"name": name, "url": url, "status": "skipped", "error": f"File not found: {file_path}"}

    try:
        # Navigate to the document detail page
        await safe_goto(page, url)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1000)

        logger.debug(f"  Page URL: {page.url}")

        # Step 1: Click the "Replace" button on the detail page
        replace_button = page.get_by_role("button", name=SELECTORS["replace_button"])
        await replace_button.click(timeout=upload_cfg["upload_timeout_ms"])

        # Step 2: Set the file on the file input (bypasses the file chooser dialog)
        file_input = page.get_by_role("button", name=SELECTORS["file_input_button"], exact=True)
        await file_input.set_input_files(file_path, timeout=upload_cfg["upload_timeout_ms"])

        # Step 3: Confirm the replacement in the dialog
        confirm_button = page.get_by_role("dialog").get_by_role("button", name=SELECTORS["replace_button"])
        await confirm_button.click(timeout=upload_cfg["upload_timeout_ms"])

        # Wait for the server to process the upload
        await page.wait_for_timeout(upload_cfg["post_upload_wait_ms"])

        return {"name": name, "url": url, "status": "success", "error": None}

    except Exception as e:
        return {"name": name, "url": url, "status": "failed", "error": str(e)}


async def upload_all_documents(page, documents: list[dict], upload_cfg: dict) -> list[dict]:
    """Iterate all documents and upload each one with retry logic."""
    results = []
    total = len(documents)
    max_retries = upload_cfg["max_retries"]
    retry_delay = upload_cfg["retry_delay_ms"]

    for i, doc in enumerate(documents):
        logger.info(f"[{i + 1}/{total}] Uploading: {doc['name']}")

        result = None
        for attempt in range(1, max_retries + 1):
            result = await upload_single_document(page, doc, upload_cfg)

            if result["status"] in ("success", "skipped"):
                break

            logger.warning(f"  Attempt {attempt}/{max_retries} failed: {result['error']}")
            if attempt < max_retries:
                logger.info(f"  Retrying in {retry_delay}ms...")
                await page.wait_for_timeout(retry_delay)

        results.append(result)

        if result["status"] == "success":
            logger.info("  OK")
        elif result["status"] == "skipped":
            logger.warning(f"  SKIPPED: {result['error']}")
        else:
            logger.error(f"  FAILED: {result['error']}")

    return results


def log_summary(results: list[dict]) -> None:
    """Log a summary of upload results."""
    success = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]

    logger.info("")
    logger.info("=== UPLOAD SUMMARY ===")
    logger.info(f"Total:   {len(results)}")
    logger.info(f"Success: {len(success)}")
    logger.info(f"Failed:  {len(failed)}")
    logger.info(f"Skipped: {len(skipped)}")

    if failed:
        logger.info("")
        logger.info("--- Failed documents ---")
        for r in failed:
            logger.info(f"  {r['name']}: {r['error']}")

    if skipped:
        logger.info("")
        logger.info("--- Skipped documents ---")
        for r in skipped:
            logger.info(f"  {r['name']}: {r['error']}")


async def run() -> None:
    config = load_config()
    portal = config["portal"]
    browser_cfg = config["browser"]
    input_cfg = config["input"]
    upload_cfg = config["upload"]
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

        # Phase 4: Read document records from Excel
        input_file = str(SCRIPT_DIR / input_cfg["filename"])
        documents = read_document_records(input_file, input_cfg["sheet_name"])

        logger.info(f"Documents to upload: {len(documents)}")

        # Phase 5: Upload all documents sequentially
        results = await upload_all_documents(page, documents, upload_cfg)

        # Phase 6: Log summary
        log_summary(results)

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
