# Maps "no website" lead finder

Sweeps an area on Google Maps for a business category and exports the
businesses that have **no real website** to a multi-sheet Excel file, ready for
outreach.

## Problem

Web agencies and freelancers want a list of local businesses that have no
website (or only a Facebook page or a booking-platform profile). Google Maps holds
that information, but a single Maps search stops at about 120 results, the
"Website" button in the result list is unreliable, and a scraper that silently
misreads the page produces a clean-looking list of false leads.

## How it works

1. **Geocode**: `--location` becomes a lat/lng through OpenStreetMap
   Nominatim. The first result is pinned in SQLite so later resumes use the same
   centre.
2. **Tile**: a square grid of search positions covers the radius (longitude is
   corrected for latitude). Cells whose centre falls outside the circle are
   dropped.
3. **Harvest (phase 1)**: each tile runs the query at `@lat,lng,zoom`, scrolls the
   results feed until no new results arrive, and collects each place's feature
   id from its link. Duplicates across overlapping tiles are dropped at insert
   time, before they cost a page load.
4. **Adaptive subdivision**: a tile that returns at the ~120 cap is split into 4
   smaller tiles one zoom level deeper. The data shows where the area is dense,
   so no zoom level has to be guessed.
5. **Detail (phase 2)**: each unique place is opened once. The website field is
   read from the JSON payload that Google embeds in the page. A DOM check runs
   when the website comes back empty.
6. **Classify**: the URL is sorted into `none`, `social_only`, `ordering_platform`
   or `real` by matching domains (exact or subdomain).
7. **Export**: the `.xlsx` has sheets *No Website*, *Social Only*, *Ordering
   Platform* and *All Results*, sorted by review count. The workbook is
   rewritten periodically and in a `finally` block, so an interrupted run still
   leaves usable output.

Both phases are resumable. SQLite records the harvested tiles (per query) and
each place's state (`pending` / `done` / `failed`). A place that fails to load
is counted as `failed` and never becomes a "no website" lead.

## Playwright techniques

- **Patchright persistent context** (`launch_persistent_context`, real Chrome
  channel, `no_viewport=True`). There are no UA, viewport or locale overrides,
  so the browser profile stays realistic and consistent. The profile keeps the
  consent answer between runs.
- **EU consent wall handling**: every navigation goes through one `safe_goto()`
  helper. It clicks "Reject all" (with locale variants) and falls back to
  submitting the consent form through `page.evaluate`, then re-navigates.
  Consent is cleared *before* challenge detection runs.
- **Back off on a challenge**: a cheap URL check for `/sorry/` runs first,
  then a body-text scan. `ChallengeDetected` stops the run (progress is saved)
  instead of retrying.
- **Reading the embedded JSON instead of the DOM**: an in-page JS walker collects
  every XSSI-guarded string under `window.APP_INITIALIZATION_STATE`.
  `page.wait_for_function` polls for the async-injected payload instead of
  sleeping a fixed time.
- **Shape-validated record locator**: known paths are tried first, then a
  bounded blind search that checks the shape (feature-id regex at `[10]`, name
  at `[11]`). When Google moves the record again, it logs `PAYLOAD DRIFT`
  instead of breaking.
- **Infinite-scroll harvesting**: the scroll loop stops on a *count plateau* of
  result links, not on `scrollHeight`, with tuned backoff (a plateau needs at
  least ~3 s of quiet). Two link selectors are merged so that one rotating away
  does not cause an outage.
- **All selectors and JSON offsets live in `selectors.py`**, so a Google layout
  change needs a fix in one file only.
- **Canary script**: before a session, `scripts/canary.py` checks well-known
  listings with stable websites. Silent offset rot shows up as "every business
  has no website", and the canary catches that.
- Jittered pacing and concurrency 1. A challenge page stops the run instead of
  retrying.

## How AI was used

I built this with Claude Code. The project folder kept the workflow's
artifacts:

- **Plan first, tracer bullets**: a checklist plan (`tasks/todo.md`) split the
  build into end-to-end slices. The order was: live website extraction, then
  feed harvest, single search to `.xlsx`, SQLite resume, grid tiling, and
  finally canary and offline tests. Each slice was tested against the live site
  before the next one started.
- **Deviation log**: `implementation-notes.md` records where live testing proved
  the plan wrong, and what changed as a result:
  - the EU consent wall, not selector rot, was the real blocker;
  - the payload and record locations had moved away from every published guide,
    which led to the shape-validated locator;
  - the payload is injected asynchronously;
  - link-in-bio and booking domains were misclassified.
- **Iterative test/fix**: the offline test suite (60 tests) and the canary
  came out of that loop.

Those planning notes are not included here because they reference real
scraped data.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
patchright install chrome

export NOMINATIM_USER_AGENT="my-app/1.0 (you@example.com)"   # see .env.example

python scripts/canary.py                                   # run first: exit 0 = safe to scrape

python -m src.main --query "hair salon" --location "Utrecht, NL" --max-places 20
python -m src.main --query "hair salon" --location "Utrecht, NL" --radius-km 5
python -m src.main --query "hair salon" "nail salon" --lat 52.09 --lng 5.12 --radius-km 3
python -m src.main ... --retry-failed      # re-queue places that failed to load

python -m pytest tests -q                  # offline, no browser needed
```

Output goes to `data/leads.xlsx` and the resume database to `data/places.db`.
Both paths can be changed with `--out` and `--db`.

| Env var | Purpose |
|---|---|
| `NOMINATIM_USER_AGENT` | Identifying User-Agent for Nominatim geocoding (their usage policy requires one). Only used with `--location`. |

## Files

| Path | Role |
|---|---|
| `src/main.py` | CLI, geocode + centre pinning, run modes |
| `src/pipeline.py` | Orchestration: harvest, subdivide, detail, classify, export |
| `src/geo.py` | Nominatim geocoding, grid tiling, adaptive subdivision |
| `src/store.py` | SQLite resume state (places, details, tiles, pinned centre) |
| `src/classify.py` | URL to prospect tier: none / social_only / ordering_platform / real (Google-builder sites count as social_only unless `--builders-are-real`) |
| `src/excel.py` | Multi-sheet `.xlsx` writer, phone/CID kept as text |
| `src/models.py` | `Place`, `PlaceDetail`, `WebsiteTier` |
| `src/logging_conf.py` | UTF-8-safe console + file logging |
| `src/scraper/selectors.py` | Every CSS selector and JSON offset, plus the record locator |
| `src/scraper/feed.py` | Phase 1: search URL, feed scroll, feature-id harvest |
| `src/scraper/detail.py` | Phase 2: payload extraction, record parsing, DOM cross-check |
| `src/scraper/consent.py` | Consent wall + `safe_goto()` navigation gateway |
| `src/scraper/browser.py` | Patchright persistent session, challenge detection |
| `src/scraper/errors.py` | `ChallengeDetected`: stop the run, never retry into a block |
| `src/scraper/timing.py` | Jittered delays |
| `scripts/canary.py` | Pre-flight check for payload/offset rot |
| `tests/` | Offline tests: classifier, geo tiling, store/resume, Excel, parser on a synthetic record |

## Responsible use

- **Terms of service**: automated scraping of Google Maps is against Google's
  Terms of Service, and the official Places API terms forbid storing results or
  exporting them to a spreadsheet. Treat this as a demonstration of technique;
  check the terms that apply to you before running it. The code runs at
  concurrency 1 with jittered delays and stops at the first challenge page.
- **Personal data**: the export contains business names, phone numbers and
  addresses. For sole traders these are personal data under the GDPR. You need
  a lawful basis (for B2B outreach, usually legitimate interest) before you
  contact anyone. Keep the file private, store only what you use, delete it when
  you are done, and honour opt-out requests.
