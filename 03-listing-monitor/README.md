# Real-estate listing monitor

A scheduled Playwright bot that watches a real-estate listing site. It records
new listings in a Google Sheet, tracks each listing until it sells or is
withdrawn, and sends Telegram alerts.

## Problem

Buyers and analysts want to know the moment a matching home is listed, and how
quickly homes in an area sell. The listing site offers no API or alerts for
this. The monitor has to run unattended several times a day. It must not
confuse "the page did not load properly" with "no new listings", and it must
not report a listing as sold just because a crawl was cut short.

## How it works

1. **Load state**: open the Google Sheet (service account), write headers if
   missing, and load every known listing id from column A. An empty sheet
   switches to backfill mode (up to 50 pages).
2. **Browser session**: launch Chromium with stealth patches, a rotated browser
   fingerprint and, optionally, a proxy from the pool. Dismiss the cookie
   banner.
3. **Paginated search** (`/search?location=…&min_price=…&page=N`): go through
   the results sorted newest first. On each page, extract cards with an
   in-page JS function and parse price, area, bedrooms, rating and postal code.
   Drop cards from cities outside the configured areas. Stop early after N
   consecutive known ids.
4. **Empty vs. failed page**: a page with zero cards is classified before it is
   trusted. A verification page, HTTP 403/429/503, or missing results text all
   raise `BlockedError`. Only a provably empty results page ends pagination
   normally.
5. **Detail pages**: for each new listing, read all `<dt>/<dd>` pairs (year
   built, heating, date listed, …). Each URL is retried on its own, and a
   failure rotates to a fresh browser context (new fingerprint and proxy).
6. **Write + notify**: insert new rows at row 2 (newest on top). Send a
   Telegram digest, split into as many messages as needed so that no listing is
   cut off.
7. **Lifecycle sweep** (once a day): reconcile the open listings in the sheet
   against (a) the site's sold view (`status=` filter) and (b) disappearance
   from a *complete* search pass. Write status, close date, days-on-market and a
   sold flag in one batch update. A sanity check skips the disappearance signal
   when most open listings are missing, since that points to a broken crawl.
8. **Report**: rebuild a per-area `summary` tab (median/mean days-to-sell,
   % sold, plus a user-maintained asking-vs-sold price delta) for a Looker
   Studio dashboard.
9. **Alerting**: a failed run exits non-zero and sends one Telegram alert per
   24 h. The cooldown is only armed after the alert has actually been
   delivered.

## Playwright techniques

- **One site profile**: every site-specific value (URL scheme, selectors,
  page-text markers, status wording, labels, date words) lives in
  `site_profile.py`. The in-page JS stays a constant and receives those values
  through `page.evaluate(js, arg)`.
- **In-page extraction with `page.evaluate`**: one JS pass builds all card
  objects (resilient `closest()` fallbacks, regexes for price, status badge and
  postal code) instead of hundreds of locator round-trips.
- **Stealth patches and fingerprint/proxy rotation to reduce bot-detection
  false positives**: playwright-stealth, `--disable-blink-features=AutomationControlled`,
  full Chromium in new headless mode, and a User-Agent derived from the running
  browser with the "HeadlessChrome" marker removed. Viewport, colour scheme and
  device scale factor vary per browser context; locale and timezone come from
  the site profile (a single locale by default).
- **Proxy rotation**: a pool with health states (healthy, suspect, cooldown
  with exponential backoff, dead) and round-robin, random or least-used
  strategies. Playwright fixes the proxy per context, so rotating means
  creating a new context (`BrowserSession.rotate()`). Credentials are masked in
  logs.
- **Retry with exponential backoff + jitter and a circuit breaker**. An
  `on_retry` hook swaps in a new page and proxy between attempts.
- **Empty vs. failed classification**: `wait_for_load_state("networkidle")`,
  a second consent check, then markers in the HTML and page title, plus the
  HTTP status.
- **Jittered pacing** between pages and detail loads.
- **Docker + supercronic** (4 runs a day), with `init: true` (tini) so the
  orphaned Chrome processes from each run get reaped.

## How AI was used

I built this with Claude Code, and the repository history shows it:

- **16 of the 18 commits** are co-authored by Claude (`Co-Authored-By` trailers).
- A **`CLAUDE.md`** with project rules guided every session. It covers the
  architecture map, commands and working rules, for example "derive the
  User-Agent, never hand-write it" and "never read a failed page as an empty
  result".
- **Iterative, production-driven fixes**: most of the later commits responded
  to real failures seen in production. Examples: telling a failed page apart
  from an empty result set, holding a failed sold view until the active pass
  had been written, throttling alerts, and reaping zombie Chrome processes.

The `CLAUDE.md` and the task notes are not included here because they describe
the private deployment.

## Build time

The first working version took **2 days, about 4.5 hours hands-on** (50 prompts). Over the following 5 months it got about 10 more hours of occasional improvements, 18 commits in total.

Measured from my Claude Code prompt history, git commits and file timestamps. "Hands-on" counts the time I spent prompting; Claude often kept working autonomously after that.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env        # set LISTING_SPREADSHEET_ID, LISTING_BASE_URL, LISTING_AREAS, ...
mkdir -p credentials        # put the Google service-account key at credentials/service_account.json

python src/main.py          # one run
```

With Docker (runs once on start, then at 06:00, 11:00, 16:00 and 21:00 UTC):

```bash
docker compose up -d --build   # set LISTING_GOOGLE_CREDENTIALS_B64 in .env instead of mounting the key
```

In production it ran as this container on a small Linux server and was
redeployed by CI on every push to `main`. That workflow is not included.

Key environment variables (all listed in `.env.example`):

| Env var | Purpose |
|---|---|
| `LISTING_SPREADSHEET_ID` | Target Google Sheet key (required) |
| `LISTING_BASE_URL` | Site root URL (default `https://listings.example.com`) |
| `LISTING_AREAS` | JSON list of locations, e.g. `["example-city"]` |
| `LISTING_PRICE_MIN` / `LISTING_PRICE_MAX` | Optional price band |
| `LISTING_GOOGLE_CREDENTIALS_PATH` / `_B64` | Service-account key path, or base64 for Docker |
| `LISTING_TELEGRAM_ENABLED` / `_BOT_TOKEN` / `_CHAT_IDS` | Telegram alerts |
| `LISTING_PROXY_*`, `LISTING_FINGERPRINT_ENABLED` | Proxy and fingerprint rotation |
| `LISTING_LIFECYCLE_SWEEP_ENABLED` | Daily sold/withdrawn reconciliation |

Everything specific to the target site is in **`src/scraper/site_profile.py`**,
filled with neutral placeholder values. Adapt that one file to the site you
monitor.

## Files

| Path | Role |
|---|---|
| `src/main.py` | Orchestrator: sheet state, browser path, retry/proxy wiring, failure alerting |
| `src/config.py` | `Settings` (pydantic-settings, `LISTING_` prefix), search URL builders |
| `src/models.py` | `ListingSummary`, `ListingDetail`, `FullListing`, sheet schema |
| `src/lifecycle.py` | Sold/withdrawn detection, days-on-market |
| `src/reporting.py` | Per-area summary tab, asking-vs-sold price delta reference |
| `src/scraper/site_profile.py` | **All site-specific values** (placeholders): URL scheme, selectors, markers, labels, formats |
| `src/scraper/browser.py` | Chromium + stealth launch, UA derivation, `BrowserSession` context rotation, cookie banner |
| `src/scraper/search_page.py` | Pagination, JS card extraction, empty-vs-failed classification, sold view |
| `src/scraper/detail_page.py` | `<dt>/<dd>` feature extraction |
| `src/scraper/parsers.py` | Price/area/postal/date parsing, status normalization |
| `src/scraper/fingerprint.py` | Fingerprint rotation per context |
| `src/scraper/proxy_pool.py` | Proxy health tracking and rotation strategies |
| `src/scraper/retry.py` | Backoff, jitter, circuit breaker |
| `src/scraper/timing.py` | Jittered delays |
| `src/storage/sheets.py` | gspread: headers, known ids, insert, batch lifecycle updates |
| `src/notifications/telegram.py` | Bot API sender: chunking, 429/5xx retry, alerts |
| `Dockerfile`, `docker-entrypoint.sh`, `crontab`, `docker-compose.yaml` | Container + schedule |

## Responsible use

- Only run this against a site whose terms of use and `robots.txt` permit
  automated access, or with the site owner's permission. The stealth and
  rotation features reduce false positives on normal, low-volume monitoring;
  they are not a licence to ignore a site's rules.
- Keep the request rate low: the defaults are a handful of runs a day, paced
  pages and detail loads, and early stopping once known listings repeat.
- Listing rows contain street addresses and are pushed to Google Sheets and
  Telegram. Addresses can be personal data (GDPR in the EU): keep the sheet and
  chat private, collect only the fields you need, and delete rows you no
  longer need.

## Tests

There are no automated tests in this project. The pure functions
(`parsers.py`, `lifecycle.classify_transition`, `reporting.compute_summary`) are
the natural place to add them.
