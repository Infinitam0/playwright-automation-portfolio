# 10 · ITSM asset history export + re-assign bot (iframes → "load more" → Excel → dialog fill)

## Problem

In an ITSM service desk, find the records whose assignment to an asset was removed, and restore them. The only record is the asset's event history, which lives in nested iframes of a single-page app and loads a page at a time behind "Load more". Re-assigning means walking an autocomplete dialog once per person. Two small bots cover both steps: export the removed assignments, then re-assign them.

## How it works

**Export** (`main.py`)
1. Copies the operator's signed-in Edge profile and launches Playwright on it (`shared/browser_session.py`). It opens the portal with `wait_until="commit"`. If the URL is already on the authenticated path it skips login; otherwise it clicks "Sign in" → "Continue with SSO" and waits for the redirect.
2. Waits until the SPA shell finishes loading. If the page title stays on "Loading", it re-invokes the app's own init hook through `page.evaluate`, which unsticks a known silent failure.
3. Walks the left-hand menu to the asset module by clicking the **smallest** visible div with each exact label (menu IDs are dynamic), then finds the asset-toolbar iframe by URL and opens the asset overview.
4. In the overview iframe: selects the asset type, searches the asset, and clicks it. It detects the new detail iframe by diffing `page.frames` URLs before and after the click.
5. Opens "History" and clicks "Load more" until it disappears (with a click cap).
6. Scrapes each assign/unassign event (person, performer, timestamp in `DD-MM-YYYY HH:MM` or `D Month YYYY HH:MM`), filters on event type and, optionally, one exact minute, and writes `export_unassigned.xlsx`.

**Re-assign** (`reassign_people/main.py`)
1. Reads person names from column B of the export.
2. Same login and navigation to the asset detail iframe, then waits for the "Add assignee" menu item.
3. For each person: opens the dialog, types the name, waits for autocomplete, picks the first suggestion and confirms. A failure is logged and the loop moves to the next person.

## Playwright techniques

- **SSO session reuse**: a persistent Edge context on a copied profile, including a SQLite hot-backup of the locked cookie DB (see `shared/`).
- **Iframe handling by URL**: `page.frames` polling with `find_frame_by_url`, and new-frame detection by diffing known frame URLs around a click.
- **Stall recovery**: page-title polling plus `page.evaluate` of the app's own init hook to unblock a stuck loader.
- **Dynamic-ID menus**: a `div:text-is('…'):visible` locator, choosing the element with the smallest `bounding_box()` area.
- **Exhaustive "Load more"** pagination with a bounded loop and a short visibility timeout as the stop signal.
- `data-testid` locators whose IDs come from config, and role locators (`menuitem`, `textbox`, `button`) inside frames.
- **Autocomplete form-fill**: fill the input, wait for suggestions, click the first result, confirm, pace with a configurable delay.
- Pydantic-validated YAML config. The companion bot reuses the export bot's login and navigation modules.

## How AI was used

- The DOM was explored live with an AI agent: the repo contained a `.mcp.json` configuring the **Playwright MCP** server, plus a Playwright MCP console log from the exploration session (neither included).
- The re-assign dialog was recorded with Playwright codegen and annotated with a per-person loop marker. That recording (not included) was the spec for `assign_person.py`.
- **1 of 4 commits** carries a `Co-Authored-By: Claude` trailer (a variant of the export bot for a second asset, omitted here as a duplicate).

## Build time

The export bot arrived complete in the first commit. The re-assign bot took **about 40 minutes**, from the commit of its codegen recording to the commit of the working implementation.

## Run it

**Personal data and access:** the export contains people's names. Run the bots only with your own account and with permission to automate the portal, and handle the output per GDPR (or your local equivalent).

Prerequisite: **an already-signed-in browser profile** (Edge by default). Sign in to the portal once in your normal browser, and the bot reuses that session.

```bash
pip install -r requirements.txt
playwright install msedge

cp config.example.yaml config.yaml                                    # asset type/asset IDs, search term, filter
cp reassign_people/config.example.yaml reassign_people/config.yaml
export PORTAL_BASE_URL=https://portal.example.com/                    # optional, overrides portal.base_url

python main.py                     # export unassigned people to export_unassigned.xlsx
python reassign_people/main.py     # re-assign everyone in that file
python test_scrape_export.py       # offline self-check of timestamp parsing and event classification
```

Selectors, labels, `data-testid` patterns and frame-URL fragments are placeholders, grouped in a `SELECTORS` block (or constants) at the top of `navigate_assets.py`, `scrape_export.py`, `portal_login.py` and `reassign_people/assign_person.py`. Adapt them to the target service desk.

## Files

| Path | Purpose |
|---|---|
| `main.py` | Export bot entrypoint |
| `portal_login.py` | Login and SSO pass-through, SPA load-stall recovery |
| `navigate_assets.py` | Menu clicks, iframe discovery, asset search, open detail and event history (shared by both bots) |
| `scrape_export.py` | "Load more" loop, event scraping, timestamp parsing, filter, Excel export |
| `models.py` | Pydantic models for events and both bots' configs |
| `test_scrape_export.py` | Offline asserts for parsing and classification |
| `reassign_people/main.py` | Re-assign bot entrypoint |
| `reassign_people/assign_person.py` | Autocomplete dialog fill, per-person error isolation |
| `reassign_people/read_excel.py` | Reads names from the export |
| `config.example.yaml`, `reassign_people/config.example.yaml` | Placeholder config |
| `../shared/browser_session.py` | Profile copy and persistent-context launch |
| `requirements.txt`, `.gitignore` | Dependencies; ignores config, logs and exports |
