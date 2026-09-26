# 07 · Template export + bulk re-upload (virtualized grid → files → Excel → upload)

## Problem

An admin portal behind SSO holds a large set of document templates. It can only download or replace them one at a time, from a detail page reached through a virtualized AG-Grid. The job was to pull every active template to disk with an index in Excel, edit the files offline, and push the edited versions back in bulk.

## How it works

**Export** (`main.py`)
1. Copies the operator's signed-in Edge profile to a temp dir and launches Playwright on it (`shared/browser_session.py`). Clicks through the SSO page if one is shown, then warms up the app domain, which is separate from the SSO entry domain.
2. Follows the menu path (a placeholder `SELECTORS` block at the top of `main.py`) to the template grid and applies an AG-Grid floating filter (`True` on the last column) to keep only active templates.
3. Reads the header row, then reads **all** filtered rows through the AG-Grid JavaScript API (`forEachNodeAfterFilterAndSort`), which ignores row virtualization. If the API is not reachable, it scrolls the viewport step by step and collects rows by `row-index`.
4. For each row: scrolls it into view, clicks it, records the detail-page URL, downloads the template with `expect_download` and saves it to `docs/` without overwriting existing files. It returns through the "All templates" link, or `go_back()` if the link is missing.
5. After each return it compares the row count and re-applies the filter if the grid lost it.
6. Writes every grid column plus `Document URL` and `Document Path` to `templates_export.xlsx`.

**Upload** (`upload_bulk_files_sequentially/main.py`)
1. Same session setup.
2. Reads `Name`, `Document URL` and `Document Path` from the export workbook, by header name.
3. For each document: skips it if the local file is missing, opens the detail URL, clicks "Replace", sets the file with `set_input_files` (no file-chooser dialog), and confirms inside the dialog.
4. Retries failed uploads (configurable count and delay), then logs a summary of successes, failures and skips.

## Playwright techniques

- **SSO session reuse**: `launch_persistent_context(channel="msedge")` on a temp copy of a real browser profile, including the locked cookie DB (see `shared/`).
- **Beating grid virtualization** by calling the AG-Grid API through `page.evaluate`, with a DOM scroll-and-collect fallback keyed on `row-index`.
- **Downloads**: `async with page.expect_download()` plus `download.save_as()`, with collision-free file naming.
- **Uploads**: `locator.set_input_files()` on the file input, so no OS dialog.
- Scoped locators such as `get_by_role("dialog").get_by_role("button", ...)` to pick the confirm button inside the modal and not the identical one on the page.
- **Self-healing navigation**: a fallback to `go_back()` and a check that re-applies the filter after each detail-page round trip.
- `safe_goto` retries `net::ERR_ABORTED`, which happens when an SSO redirect interrupts a navigation.
- Per-item error isolation: a failed download is logged and the export moves on; uploads also get a retry loop that records skipped and failed items without aborting the batch.

## How AI was used

- The original repo had a `CLAUDE.md` project guide for Claude Code (not included). It documents the architecture, the AG-Grid API-first/DOM-fallback approach and the two-domain SSO warm-up.
- The menu path came from a Playwright codegen recording (not included), which served as the spec for the navigation code.
- The repo was bulk-imported in a single commit, and **0 of 1 commits** carries a `Co-Authored-By: Claude` trailer, so git history gives no finer evidence or build time.

## Run it

Prerequisite: **an already-signed-in browser profile** (Edge by default). Sign in to the portal once in your normal browser, and the bot reuses that session.

```bash
pip install -r requirements.txt
playwright install msedge

cp config.example.yaml config.yaml                                   # URLs, SSO button label, account name
cp upload_bulk_files_sequentially/config.example.yaml upload_bulk_files_sequentially/config.yaml
export PORTAL_BASE_URL=https://portal.example.com/                   # optional, overrides portal.entry_url

python main.py                                   # export: docs/ + templates_export.xlsx
python upload_bulk_files_sequentially/main.py    # upload the (edited) files back
```

Menu labels, button labels and the filter value are placeholders in the `SELECTORS` blocks at the top of both `main.py` files; button names are matched by regex because the portal prefixes them with an icon glyph. Adapt them to the target portal.

## Files

| Path | Purpose |
|---|---|
| `main.py` | Export bot: navigation, AG-Grid API/DOM row extraction, per-row detail capture and download |
| `export_excel.py` | Writes grid columns plus URL and local path to `.xlsx` |
| `upload_bulk_files_sequentially/main.py` | Upload bot: Replace flow with `set_input_files`, retries, summary |
| `upload_bulk_files_sequentially/import_excel.py` | Reads the export workbook by header name, skips incomplete rows |
| `config.example.yaml`, `upload_bulk_files_sequentially/config.example.yaml` | Placeholder config (portal URLs, browser, output/input, retry settings) |
| `../shared/browser_session.py` | Profile copy, persistent-context launch, SSO pass-through, `safe_goto` |
| `requirements.txt`, `.gitignore` | Dependencies; ignores config, logs, exports and `docs/` |
