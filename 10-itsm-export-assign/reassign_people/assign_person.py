import logging

from playwright.async_api import Frame, Page

logger = logging.getLogger(__name__)

# Selectors and UI labels are placeholders; adapt them to the target portal.
ASSIGN_MENU_ITEM = "Add assignee"
SELECTORS = {
    "person_field_link": "#assignee-field a",  # activates the person search
    "person_textbox": "Assignee",  # accessible name of the search textbox
    "suggestion": "#assignee-field div",  # autocomplete rows; nth(2) is the first result
    "confirm_button": "Assign",
}


async def wait_for_assign_menu(frame: Frame) -> None:
    """Wait until the asset detail frame offers the 'Add assignee' action."""
    logger.info(f"Waiting for '{ASSIGN_MENU_ITEM}' menuitem to be available...")
    await frame.get_by_role("menuitem", name=ASSIGN_MENU_ITEM).wait_for(state="visible", timeout=30000)
    logger.info(f"Asset detail frame ready with '{ASSIGN_MENU_ITEM}' available.")


async def assign_person(frame: Frame, person_name: str, page: Page, delay_ms: int) -> None:
    """Assign a single person via the portal's 'Add assignee' dialog."""
    # 1. Click the "Add assignee" menuitem
    await frame.get_by_role("menuitem", name=ASSIGN_MENU_ITEM).click()

    # 2. Click the person field link to activate person search
    await frame.locator(SELECTORS["person_field_link"]).click()

    # 3. Fill person name in textbox
    await frame.get_by_role("textbox", name=SELECTORS["person_textbox"]).fill(person_name)

    # 4. Wait for autocomplete dropdown to populate
    await page.wait_for_timeout(1000)

    # 5. Select the first autocomplete result
    await frame.locator(SELECTORS["suggestion"]).nth(2).click()

    # 6. Confirm the assignment
    await frame.get_by_role("button", name=SELECTORS["confirm_button"]).click()

    # 7. Wait between assignments to let the UI settle
    await page.wait_for_timeout(delay_ms)


async def assign_all_persons(frame: Frame, person_names: list[str], page: Page, delay_ms: int) -> None:
    """Assign all persons from the list, continuing on individual failures."""
    total = len(person_names)
    success_count = 0
    fail_count = 0

    for i, name in enumerate(person_names, 1):
        logger.info(f"Assigning person {i}/{total}")
        logger.debug(f"Person {i}/{total}: {name}")
        try:
            await assign_person(frame, name, page, delay_ms)
            success_count += 1
            logger.info(f"Assigned person {i}/{total}")
        except Exception as e:
            fail_count += 1
            # Playwright errors can echo the typed name, so full details go to DEBUG only
            logger.error(f"Failed to assign person {i}/{total}: {type(e).__name__}")
            logger.debug(f"Person {i}/{total} ({name}) failed: {e}")
            # Continue with next person

    logger.info(f"Assignment complete: {success_count} succeeded, {fail_count} failed out of {total}")
