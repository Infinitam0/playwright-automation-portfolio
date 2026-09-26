"""
calendar_scraper.py

Calendar navigation and appointment data extraction from the portal's
calendar app, using Playwright native locators.

All quoted button/link/heading texts are UI labels on the target site.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Selectors are placeholders; adapt them to the target portal's DOM.
SELECTORS = {
    "app_switcher_testid": "app-switcher",  # data-testid of the app-switcher menu
    "day_column": "div.calendar-day",  # one per visible day, carries data-date="YYYY-MM-DD"
    "appointment": "div.calendar-event",  # appointment block inside a day column, carries data-id
    "time_category": "div.time-category p",  # time category value in the popup
    "popup_description": "div.event-description",  # fallback for the time category
    "attendee_list": "div.attendee-list",  # multi-attendee popups: one <a> per attendee
    "single_attendee": "td.attendee h5",  # single-attendee popups
}
WEEK_READY_JS = f"document.querySelectorAll('{SELECTORS['day_column']}').length >= 7"

# Time table rows in the appointment popup: UI label -> Excel column
TIME_FIELDS = {
    "Category A": "Time_CategoryA",
    "Category B": "Time_CategoryB",
    "Category C": "Time_CategoryC",
    "Category D": "Time_CategoryD",
}


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------


async def navigate_to_user_calendar(page, calendar_url: str, user_code: str, start_date: str) -> None:
    """Navigate from the calendar URL to a specific user's calendar at a specific date.

    Steps:
    1. Navigate to calendar_url
    2. Click app-switcher menu item -> "Calendar" link
    3. Click "Add calendar" -> "User"
    4. Search for the user by code, select first result
    5. Open date picker, select year/month/day-1, click "Go to"

    Args:
        page: Playwright page object
        calendar_url: URL of the calendar app
        user_code: User search code string
        start_date: YYYY-MM-DD string; the year and month are used for the date picker
    """
    logger.info("Navigating to calendar URL: %s", calendar_url)
    await page.goto(calendar_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    # Step 2: Open app-switcher and click Calendar
    logger.info("Opening app-switcher menu")
    await page.get_by_test_id(SELECTORS["app_switcher_testid"]).click()
    await page.wait_for_timeout(1000)
    await page.get_by_role("link", name="Calendar").click()
    await page.wait_for_timeout(2000)

    # Step 3: Add a calendar for a user
    logger.info("Clicking 'Add calendar'")
    await page.get_by_role("button", name="Add calendar").click()
    await page.wait_for_timeout(1000)
    await page.get_by_role("button", name="User", exact=True).click()
    await page.wait_for_timeout(1000)

    # Step 4: Search for the user and select first result
    logger.info("Searching for user: %s", user_code)
    search_box = page.get_by_role("textbox", name="Search users")
    await search_box.click()
    await search_box.fill(user_code)
    # Wait for search results to load
    await page.wait_for_timeout(2000)

    # The codegen recording shows results inside a container labeled "Search by name..."
    # Try several selector strategies in order of preference.
    results_container = page.get_by_label("Search by name...")
    selected = False

    # Strategy 1: list items (most common dropdown pattern)
    try:
        first_li = results_container.locator("li").first
        if await first_li.count() > 0 and await first_li.is_visible(timeout=3000):
            logger.debug("Selecting first search result via <li>")
            await first_li.click()
            selected = True
    except Exception as e:
        logger.debug("Strategy 1 (li) failed: %s", e)

    # Strategy 2: role=option (ARIA combobox pattern)
    if not selected:
        try:
            first_option = results_container.get_by_role("option").first
            if await first_option.count() > 0 and await first_option.is_visible(timeout=3000):
                logger.debug("Selecting first search result via role=option")
                await first_option.click()
                selected = True
        except Exception as e:
            logger.debug("Strategy 2 (role=option) failed: %s", e)

    # Strategy 3: any div or span with non-empty text (fallback)
    if not selected:
        try:
            first_text = results_container.locator("span, div").filter(has_text=re.compile(r".+")).first
            if await first_text.count() > 0 and await first_text.is_visible(timeout=3000):
                logger.debug("Selecting first search result via span/div text fallback")
                await first_text.click()
                selected = True
        except Exception as e:
            logger.debug("Strategy 3 (span/div text) failed: %s", e)

    if not selected:
        raise RuntimeError(
            f"Could not select search result for user code '{user_code}'. "
            "No matching result element found in the search dropdown."
        )

    logger.info("User selected")
    await page.wait_for_timeout(1500)

    # Step 5: Open date picker and navigate to start date
    await _select_date_in_picker(page, start_date)

    # Step 6: Ensure the calendar is in the full 7-day Week view so that
    # Saturday and Sunday appointments are not silently skipped.
    await ensure_full_week_view(page)


async def navigate_to_date(page, target_date: str) -> None:
    """Open the date picker and navigate to a specific date.

    Used when resuming from a specific date after initial setup is already done.

    Args:
        page: Playwright page object
        target_date: YYYY-MM-DD string for the target date
    """
    logger.info("Navigating date picker to: %s", target_date)
    await _select_date_in_picker(page, target_date)


async def _select_date_in_picker(page, target_date: str) -> None:
    """Internal helper: open the date picker and select year/month/day-1, then click 'Go to'.

    The calendar always navigates to the week containing day 1 of the given
    year/month, which is the correct starting point for weekly iteration.

    Args:
        page: Playwright page object
        target_date: YYYY-MM-DD string; only year and month are used
    """
    # Parse year and month from the target date (day is always set to 1)
    parts = target_date.split("-")
    year_str = parts[0]
    # Months in the date picker are 0-indexed (January=0, December=11)
    month_index = str(int(parts[1]) - 1)

    logger.debug("Opening date picker (year=%s, month_index=%s)", year_str, month_index)

    # The codegen recording names the button with an icon prefix, so match by regex
    await page.get_by_role("button", name=re.compile(r"Select date")).click()
    await page.wait_for_timeout(1000)

    await page.get_by_label("Select year").select_option(year_str)
    await page.wait_for_timeout(500)
    await page.get_by_label("Select month").select_option(month_index)
    await page.wait_for_timeout(500)

    # Click day "1" link - use .first to be safe in case multiple are visible
    await page.get_by_role("link", name="1", exact=True).first.click()
    await page.wait_for_timeout(500)

    await page.get_by_role("button", name="Go to").click()
    await page.wait_for_timeout(2000)
    logger.info("Date picker: navigated to %s-%s-01", year_str, parts[1])


async def ensure_full_week_view(page) -> None:
    """Switch the calendar to the 7-day "Week" view if it is not already active.

    The calendar offers at least three view modes - "Day", "Work week"
    (Mon-Fri only, 5 days), and "Week" (full 7-day view). The scraper must
    operate in the 7-day view so that Saturday and Sunday appointments are
    visible. This function is idempotent: if the calendar is already in
    "Week" view the click is either skipped or has no visible effect.

    The function uses ``exact=True`` on every locator strategy that matches
    by text so that "Work week" buttons are never accidentally matched.

    Args:
        page: Playwright page object (must already be on the calendar view)
    """
    logger.info("Ensuring calendar is in 7-day Week view")

    # Check how many day columns are currently rendered. If there are already
    # 7 we are already in full-week view and can skip the click.
    current_count = await page.locator(SELECTORS["day_column"]).count()
    if current_count == 7:
        logger.info("Calendar is already in 7-day Week view (%d day columns found)", current_count)
        return

    logger.info(
        "Calendar shows %d day column(s) (expected 7); attempting to switch to Week view",
        current_count,
    )

    clicked = False

    # Strategy 1: role=button with exact text "Week"
    # exact=True prevents "Work week" from matching.
    if not clicked:
        try:
            btn = page.get_by_role("button", name="Week", exact=True)
            if await btn.count() > 0 and await btn.first.is_visible(timeout=3000):
                logger.info("Clicking 'Week' button (role=button, exact)")
                await btn.first.click()
                try:
                    await page.wait_for_function(
                        WEEK_READY_JS,
                        timeout=10000,
                    )
                except Exception:
                    await page.wait_for_timeout(2000)
                clicked = True
        except Exception as e:
            logger.debug("Strategy 1 (role=button exact 'Week') failed: %s", e)

    # Strategy 2: role=link with exact text "Week"
    if not clicked:
        try:
            lnk = page.get_by_role("link", name="Week", exact=True)
            if await lnk.count() > 0 and await lnk.first.is_visible(timeout=3000):
                logger.info("Clicking 'Week' link (role=link, exact)")
                await lnk.first.click()
                try:
                    await page.wait_for_function(
                        WEEK_READY_JS,
                        timeout=10000,
                    )
                except Exception:
                    await page.wait_for_timeout(2000)
                clicked = True
        except Exception as e:
            logger.debug("Strategy 2 (role=link exact 'Week') failed: %s", e)

    # Strategy 3: role=tab with exact text "Week"
    if not clicked:
        try:
            tab = page.get_by_role("tab", name="Week", exact=True)
            if await tab.count() > 0 and await tab.first.is_visible(timeout=3000):
                logger.info("Clicking 'Week' tab (role=tab, exact)")
                await tab.first.click()
                try:
                    await page.wait_for_function(
                        WEEK_READY_JS,
                        timeout=10000,
                    )
                except Exception:
                    await page.wait_for_timeout(2000)
                clicked = True
        except Exception as e:
            logger.debug("Strategy 3 (role=tab exact 'Week') failed: %s", e)

    # Strategy 4: any element whose trimmed text is exactly "Week"
    # Uses a regex anchored to the full string so "Work week" does not match.
    if not clicked:
        try:
            el = page.locator("a, button, span, li").filter(has_text=re.compile(r"^Week$")).first
            if await el.count() > 0 and await el.is_visible(timeout=3000):
                logger.info("Clicking 'Week' element (text regex ^Week$)")
                await el.click()
                try:
                    await page.wait_for_function(
                        WEEK_READY_JS,
                        timeout=10000,
                    )
                except Exception:
                    await page.wait_for_timeout(2000)
                clicked = True
        except Exception as e:
            logger.debug("Strategy 4 (text regex ^Week$) failed: %s", e)

    if not clicked:
        logger.warning(
            "Could not find a 'Week' view toggle button. "
            "Weekend appointments may be missing from the export. "
            "Continuing with current view (%d day column(s)).",
            current_count,
        )
        return

    # Verify that the view has switched to 7 columns.
    new_count = await page.locator(SELECTORS["day_column"]).count()
    if new_count == 7:
        logger.info("Successfully switched to 7-day Week view")
    else:
        logger.warning(
            "Clicked 'Week' toggle but day column count is %d (expected 7). Weekend appointments may still be missing.",
            new_count,
        )


# ---------------------------------------------------------------------------
# Week / day inspection
# ---------------------------------------------------------------------------


async def get_week_dates(page) -> list[str]:
    """Get all dates visible in the current week view.

    Returns:
        Sorted list of YYYY-MM-DD date strings found in the current week view.
    """
    day_divs = page.locator(SELECTORS["day_column"])
    count = await day_divs.count()

    dates: list[str] = []
    for i in range(count):
        div = day_divs.nth(i)
        date_attr = await div.get_attribute("data-date")
        if date_attr:
            dates.append(date_attr)

    dates.sort()
    logger.debug("Week dates found: %s", dates)
    return dates


async def get_day_appointments(page, date: str) -> list:
    """Get all appointment locators for a specific date.

    Args:
        page: Playwright page object
        date: YYYY-MM-DD date string

    Returns:
        List of Playwright Locator objects, one per appointment div.
    """
    day_locator = page.locator(f'{SELECTORS["day_column"]}[data-date="{date}"]')
    appointments = day_locator.locator(SELECTORS["appointment"])
    count = await appointments.count()
    return [appointments.nth(i) for i in range(count)]


# ---------------------------------------------------------------------------
# Appointment detail extraction
# ---------------------------------------------------------------------------


async def extract_appointment_details(page, appointment_locator, date: str) -> list[dict]:
    """Click an appointment, extract all detail fields from the popup, then close it.

    Appointments may have multiple attendees. This function returns one dict per
    attendee, all sharing the same appointment-level fields (time, time category,
    etc.). Uses native Playwright locators throughout.

    Args:
        page: Playwright page object
        appointment_locator: Locator pointing to the appointment div
        date: YYYY-MM-DD date string for this appointment

    Returns:
        List of dicts (one per attendee) with keys matching ExcelWriter COLUMNS.
    """
    # Extract the unique appointment ID from the DOM before clicking
    appointment_id = await appointment_locator.get_attribute("data-id") or ""

    await appointment_locator.click()
    await page.wait_for_timeout(1000)

    # Shared fields (same for every attendee in this appointment)
    shared: dict = {
        "Appointment_ID": appointment_id,
        "Date": date,
        "Time": "",
        "Time category": "",
        **{column: "" for column in TIME_FIELDS.values()},
        "Created by": "",
        "Created on": "",
    }

    # --- Time ---
    try:
        time_el = page.locator("text=/Time\\s+\\d{2}:\\d{2}/").first
        if await time_el.is_visible(timeout=3000):
            text = await time_el.inner_text()
            match = re.search(r"(\d{2}:\d{2}\s*[–\-]\s*\d{2}:\d{2})", text)
            if match:
                shared["Time"] = match.group(1).strip()
    except Exception as e:
        logger.debug("Time extraction failed: %s", e)

    # --- Time category ---
    # Primary: the value paragraph under the "Time category" heading
    try:
        category_p = page.locator(SELECTORS["time_category"]).first
        if await category_p.count() > 0 and await category_p.is_visible(timeout=2000):
            text = (await category_p.inner_text()).strip()
            if text:
                shared["Time category"] = text
    except Exception as e:
        logger.debug("Time category extraction failed: %s", e)

    # Fallback: the popup description (multi-attendee popups)
    if not shared["Time category"]:
        try:
            desc_el = page.locator(SELECTORS["popup_description"]).first
            if await desc_el.count() > 0 and await desc_el.is_visible(timeout=1000):
                text = (await desc_el.inner_text()).strip()
                if text:
                    shared["Time category"] = text
        except Exception as e:
            logger.debug("Time category description fallback failed: %s", e)

    # --- Time table rows ---
    for label, column in TIME_FIELDS.items():
        try:
            # Exact row-header match first: a label can be a substring of another
            # (e.g. "Paid" in "Unpaid"), which a plain name match would confuse.
            row = page.get_by_role("rowheader", name=label, exact=True).locator("xpath=ancestor::tr")
            if await row.count() == 0:
                row = page.get_by_role("row", name=re.compile(rf"{re.escape(label)}\s+\d"))

            if await row.count() > 0:
                cell = row.first.get_by_role("cell").first
                if await cell.is_visible(timeout=2000):
                    cell_text = await cell.inner_text()
                    shared[column] = cell_text.strip()
        except Exception as e:
            logger.debug("%s extraction failed: %s", column, e)

    # --- Created on / by ---
    try:
        created_el = page.locator("text=/Created on/").first
        if await created_el.is_visible(timeout=2000):
            text = await created_el.inner_text()
            by_match = re.search(r"Created on\s+(.+?)\s+by\s+(.+)", text, re.DOTALL)
            if by_match:
                shared["Created on"] = by_match.group(1).strip()
                shared["Created by"] = by_match.group(2).strip()
            else:
                date_match = re.search(r"Created on\s+(.+)", text, re.DOTALL)
                if date_match:
                    shared["Created on"] = date_match.group(1).strip()
    except Exception as e:
        logger.debug("Created on extraction failed: %s", e)

    # --- Extract attendees ---
    # Multi-attendee appointments show an "Attendees" heading next to a list
    # with one <a> per attendee. Single-attendee popups show a name header.
    attendees: list[str] = []

    # Strategy 1: multi-attendee format - "Attendees" section with links
    try:
        attendees_heading = page.locator("h3", has_text=re.compile(r"^Attendees$"))
        if await attendees_heading.count() > 0:
            attendee_list = attendees_heading.locator("xpath=..").locator(SELECTORS["attendee_list"])
            if await attendee_list.count() > 0:
                attendee_links = attendee_list.locator("a")
                link_count = await attendee_links.count()
                for j in range(link_count):
                    name = (await attendee_links.nth(j).inner_text()).strip()
                    if name:
                        attendees.append(name)
                if attendees:
                    logger.debug("Multi-attendee popup: found %d attendee(s)", len(attendees))
    except Exception as e:
        logger.debug("Multi-attendee extraction failed: %s", e)

    # Strategy 2: single-attendee format - name header
    if not attendees:
        try:
            name_el = page.locator(SELECTORS["single_attendee"]).first
            if await name_el.count() > 0 and await name_el.is_visible(timeout=3000):
                text = (await name_el.inner_text()).strip()
                if text:
                    attendees.append(text)
        except Exception as e:
            logger.debug("Single-attendee extraction failed: %s", e)

    # Fallback: no attendee detected - still produce one row
    if not attendees:
        attendees.append("")

    # --- Close the popup ---
    try:
        await page.get_by_role("button", name="Close").click()
        await page.wait_for_timeout(500)
    except Exception as e:
        logger.warning("Failed to close appointment popup: %s", e)

    # Build one result dict per attendee
    results: list[dict] = []
    for attendee in attendees:
        row = dict(shared)
        row["Attendee name"] = attendee
        results.append(row)

    return results


# ---------------------------------------------------------------------------
# Main scraping loop
# ---------------------------------------------------------------------------


async def scrape_calendar(page, start_date: str, end_date: str, excel_writer) -> int:
    """Iterate calendar weeks and extract appointment data.

    Iterates week-by-week from the current calendar view until end_date is
    passed. Skips dates before effective_start (derived from existing Excel
    data for resumability) and skips appointments already present in the
    Excel file.

    Args:
        page: Playwright page object (must already be on the calendar view)
        start_date: YYYY-MM-DD string; overall range start
        end_date: YYYY-MM-DD string; overall range end (inclusive)
        excel_writer: ExcelWriter instance used for incremental writes and
                      duplicate detection

    Returns:
        Total number of new appointment rows exported in this run.
    """
    exported_count = 0
    skipped_count = 0
    error_count = 0
    total_found = 0

    # When resuming, skip ahead to the last exported date to avoid re-processing
    effective_start = start_date
    if excel_writer.max_exported_date:
        effective_start = excel_writer.max_exported_date
        logger.info(
            "Resuming from date: %s (found existing data up to that date)",
            effective_start,
        )

    while True:
        # Ensure 7-day view on every iteration (guards against mid-session reversion)
        await ensure_full_week_view(page)

        # --- Collect dates in the current week view ---
        week_dates = await get_week_dates(page)
        if not week_dates:
            logger.warning("No dates found in the current week view; stopping.")
            break

        logger.info("Processing week: %s to %s", week_dates[0], week_dates[-1])

        # Stop if the entire week is beyond the end date
        if week_dates[0] > end_date:
            logger.info(
                "First day of week (%s) is beyond end date (%s); stopping.",
                week_dates[0],
                end_date,
            )
            break

        # --- Process each day in the current week ---
        for date in week_dates:
            if date > end_date:
                logger.info("Reached end date %s; stopping day iteration.", end_date)
                break

            if date < effective_start:
                logger.debug("Skipping %s (before effective start %s)", date, effective_start)
                continue

            # Locate appointment divs for this day
            day_locator = page.locator(f'{SELECTORS["day_column"]}[data-date="{date}"]')
            appointments = day_locator.locator(SELECTORS["appointment"])
            count = await appointments.count()

            if count == 0:
                logger.debug("%s: no appointments", date)
                continue

            logger.info("%s: %d appointment(s) found", date, count)
            total_found += count

            for i in range(count):
                appointment = appointments.nth(i)

                try:
                    details_list = await extract_appointment_details(page, appointment, date)
                except Exception as e:
                    logger.error(
                        "Unexpected error extracting appointment %d on %s: %s",
                        i,
                        date,
                        e,
                    )
                    error_count += 1
                    # Attempt to close any open popup before continuing
                    try:
                        close_btn = page.get_by_role("button", name="Close")
                        if await close_btn.is_visible(timeout=1000):
                            await close_btn.click()
                            await page.wait_for_timeout(500)
                    except Exception:
                        pass
                    # Re-query after popup interaction to handle DOM re-renders
                    appointments = day_locator.locator(SELECTORS["appointment"])
                    continue

                # Each appointment may produce multiple rows (one per attendee)
                for details in details_list:
                    # Duplicate check using appointment ID + attendee name
                    if excel_writer.is_exported(
                        details["Appointment_ID"],
                        details["Attendee name"],
                    ):
                        logger.debug(
                            "  Skipping already exported: ID=%s attendee=%s %s %s",
                            details["Appointment_ID"],
                            details["Attendee name"],
                            date,
                            details["Time"],
                        )
                        skipped_count += 1
                        continue

                    excel_writer.append_row(details)
                    exported_count += 1
                    logger.info(
                        "  Exported: %s %s - %s",
                        date,
                        details["Time"],
                        details.get("Attendee name") or details.get("Time category") or "N/A",
                    )

                # Re-query after popup interaction to handle DOM re-renders
                appointments = day_locator.locator(SELECTORS["appointment"])

        # --- Navigate to the next week ---
        logger.info("Navigating to next week...")
        try:
            # The codegen recording names the button with an icon prefix, so match by regex
            await page.get_by_role("button", name=re.compile(r"Next")).click()
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.error("Failed to click 'Next': %s - stopping.", e)
            break

    logger.info(
        "Scraping complete. Found: %d | Exported: %d | Skipped (duplicate): %d | Errors: %d",
        total_found,
        exported_count,
        skipped_count,
        error_count,
    )
    unaccounted = total_found - exported_count - skipped_count - error_count
    if unaccounted > 0:
        logger.warning("UNACCOUNTED appointments: %d - investigate possible data loss", unaccounted)
    return exported_count
