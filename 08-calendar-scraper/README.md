# 08 · Resumable calendar scraper (week-by-week popups → incremental Excel)

## Problem

A web calendar behind SSO has no export. The job was to get one user's appointments for a date range into Excel. Each appointment's details (time, time category, time per category, who created it, and one or more attendees) only appear in a popup, and a long run must survive crashes without duplicating rows.

## How it works

1. Opens (or creates) the output workbook. If it already has rows, it builds the set of exported `appointment_id|attendee` keys and takes the latest exported date as the resume point.
2. Copies the operator's signed-in Edge profile to a temp dir, launches Playwright on it (`shared/browser_session.py`), passes through SSO and warms up the calendar domain.
3. Opens the calendar app through the app switcher and adds the user's calendar by searching the configured user code. Three selector strategies (`li`, `role=option`, any text node) pick the first search result.
4. Sets the date picker to day 1 of the start (or resume) month and forces the **7-day Week view**, so weekend appointments are not skipped. It waits with `wait_for_function` until 7 day columns are rendered.
5. For each day in the week, and each appointment in that day, it reads the appointment ID from `data-id`, opens the popup and extracts the fields. Multi-attendee popups produce one row per attendee. Then it closes the popup.
6. Each row is appended **and saved immediately**. Rows already in the file are skipped.
7. Clicks "Next" to move a week forward and repeats until the week starts after the end date. It logs found, exported, skipped and error counts, and warns about any unaccounted appointments.

## Playwright techniques

- **SSO session reuse** with a persistent context on a copied browser profile (see `shared/`).
- **`page.wait_for_function`** on a DOM condition (day-column count ≥ 7) instead of fixed sleeps after switching views.
- **Idempotent view forcing**: it checks the day-column count first, then tries four locator strategies (button, link, tab, anchored `^Week$` regex) with `exact=True`, so "Work week" is never matched.
- **Fallback selector chains** for search results, time category (value paragraph, then popup description) and attendee lists (multi-attendee links, then the single-attendee header).
- **Table parsing through ARIA roles**: `get_by_role("rowheader", exact=True)` plus an `xpath=ancestor::tr` hop (so a label that is a substring of another is not confused), falling back to `get_by_role("row", name=regex)`.
- Regex text locators (`text=/Time\s+\d{2}:\d{2}/`) and regex extraction for "Created on … by …".
- `select_option` on native `<select>` elements for year and month (the month is 0-indexed).
- Error isolation per appointment: a failed popup is closed, the locators are re-queried after DOM re-renders, and the loop continues.
- **Crash-safe, resumable output**: the workbook is saved after every row, deduplicated on a composite key, and the run restarts from the last exported date.

## How AI was used

- The code was written with Claude: **1 of 1 commits** carries a `Co-Authored-By: Claude` trailer.
- A Playwright codegen recording of the click path (not included) was the spec. Code comments reference it, for example the icon-prefixed button names matched by regex.
- The repo was imported in that single commit, so git history gives no build time.

## Run it

**Personal data:** the export contains people's names and appointment details. Run it only with authorization, and handle the output per GDPR (or your local equivalent).

Prerequisite: **an already-signed-in browser profile** (Edge by default). Sign in to the portal once in your normal browser, and the bot reuses that session.

```bash
pip install -r requirements.txt
playwright install msedge

cp config.example.yaml config.yaml                 # URLs, user code, date range
export PORTAL_BASE_URL=https://portal.example.com/ # optional, overrides portal.entry_url

python main.py                                     # re-run any time: it resumes and skips duplicates
```

CSS selectors are placeholders in the `SELECTORS` block at the top of `calendar_scraper.py`; button, heading and field labels are inline. Adapt both to the target calendar.

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entrypoint: resume detection, session setup, runs the scrape |
| `calendar_scraper.py` | Navigation, date picker, 7-day view forcing, popup extraction, week loop |
| `export_excel.py` | Incremental, resumable Excel writer (save per row, composite-key dedup, max-date resume) |
| `config.example.yaml` | Placeholder config (portal URLs, browser, user code, date range, output) |
| `../shared/browser_session.py` | Profile copy, persistent-context launch, SSO pass-through |
| `requirements.txt`, `.gitignore` | Dependencies; ignores config, logs and exports |
