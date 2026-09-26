# 05 · Signal crawler (AlternativeTo, Reddit, Hacker News → SQLite)

## Problem

Detect popular apps that have been discontinued, using the trail people leave when they go looking for a replacement: posts like *"alternative to X"*, *"X is shutting down"*, *"RIP X"*. The crawler has to cover several public platforms, respect their rate limits, store everything in one local database, and survive a hard crash (power loss, killed process) without losing or duplicating work.

## How it works

1. `scanner migrate` creates a SQLite database in WAL mode from the numbered SQL files in `migrations/`.
2. `scanner run` starts the orchestrator. It seeds one `sources` row per enabled platform (`config.toml`), recovers `running` jobs left behind by a crash, and queues one job per source, resuming from that source's saved cursor.
3. N asyncio workers claim jobs atomically from the SQLite job queue (`pending → running`).
4. Each worker instantiates the scraper for the job's source (a self-registering "atom" in `scanner/scrapers/`) and streams its `RawItem`s:
   - **Hacker News**: Algolia HN Search API over `httpx`, with no browser.
   - **Reddit**: `old.reddit.com` search pages through Playwright, paginated with `after=` tokens.
   - **AlternativeTo**: an app page per catalog entry through Playwright, checked for the "Discontinued" badge.
5. Every item passes a per-source token-bucket rate limiter and is written to `inbox` with an idempotent UPSERT keyed on `(source, source_item_id)` plus a content hash. The source cursor is committed after each item, so a restart resumes where it stopped.
6. Failures are typed. `RateLimited` (HTTP 429/403 or a captcha) defers the job with a retry-after and sets a pause flag for that source. The source stays paused until `scanner resume --source <name>` clears the flag; there is no automatic resume. `SelectorBroken` marks the job failed and flags the source for review. Any other exception fails only that job, and the worker keeps running.
7. `scanner extract` runs regex patterns and a fuzzy-matched app catalog over `inbox` to produce `signals`. The scorer then ranks candidates into `apps`. `scanner rebuild` re-derives both tables from `inbox`, so a bug in extraction or scoring never needs a re-crawl.
8. `scanner pause|resume [--source]` toggles soft-pause flags in a `control` table. SIGINT/SIGTERM finishes the current item and returns the job to `pending`.

## Playwright techniques

- Async Playwright with **one browser per scraper instance**, so a browser crash only takes down that job (`scanner/browser.py`).
- **Realistic browser profile**: a desktop Chrome user agent and viewport, with playwright-stealth applied best-effort. The code resolves either stealth API name at runtime and runs without it, logging the fact, if the installed version changed its API.
- `page.goto(url, wait_until="domcontentloaded", timeout=...)`, then **the HTTP status is read from the navigation response**: 429 honours `Retry-After`, 403 is treated as anti-bot back-off, and 404/5xx are logged and skipped.
- **Captcha detection** on `page.content()` raises a typed back-off instead of storing garbage.
- **Selector-breakage canary**: zero results on the first page of the first query raises `SelectorBroken`, so a changed layout can't pass for "nothing new".
- The page HTML goes into **pure parsing functions**, which are unit-tested offline against saved HTML/JSON snapshots in `tests/snapshots/`.
- Timeouts and navigation errors (`TimeoutError`, `Error`) are caught per page. They end one query's pagination without killing the run.

## How AI was used

- The project was built with Claude Code, working from a short written brief (a `CLAUDE.md`, not included here) that set the goals: modular scraper "atoms", SQLite storage, crash recovery, pause/resume.
- A task plan and a lessons log (`tasks/todo.md`, `tasks/lessons.md`, not included) tracked the build as nine numbered commits.
- **All 9 of 9 commits** carry a `Co-Authored-By: Claude` trailer.
- There was no `.planning/` directory for this project.

## Build time

About **half an hour** from first to last commit, across 9 commits.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

python -m scanner migrate          # create scanner.sqlite
python -m scanner run --once       # drain one pass of all sources and exit
python -m scanner status           # jobs, cursors, inbox counts, pause flags
python -m scanner extract          # inbox -> signals -> ranked apps
python -m scanner apps top         # top discontinued candidates
python -m scanner validate         # check against tests/validation_set.yaml

pytest -q                          # offline test suite, no network
```

The crawler is rate-limited per source. Use it in line with each site's terms of service and robots.txt (not enforced by code). For Reddit, real use should go through the official Data API.

Behaviour is set in `config.toml` (DB path, worker count, per-source rate limits). The scanner needs no credentials or `.env`. A `justfile` wraps the same commands.

## Files

| Path | Purpose |
|---|---|
| `scanner/browser.py` | Playwright session: launch, stealth, context options, safe close |
| `scanner/base.py` | `BaseScraper` contract plus the `RateLimited` / `SelectorBroken` exceptions |
| `scanner/registry.py` | `@register_scraper` decorator and auto-loader for `scanner/scrapers/` |
| `scanner/scrapers/reddit.py` | old.reddit.com search scraper (Playwright) |
| `scanner/scrapers/alternativeto.py` | AlternativeTo "Discontinued" badge checker (Playwright) |
| `scanner/scrapers/hackernews.py` | HN Algolia API scraper (httpx) |
| `scanner/orchestrator.py` | Worker pool, job lifecycle, pause/resume, signal handling |
| `scanner/jobqueue.py` | SQLite job queue: atomic claim, defer, stale-job recovery |
| `scanner/ratelimit.py` | Per-source token bucket |
| `scanner/db.py` | aiosqlite connection pragmas (WAL) and migration runner |
| `scanner/extract/` | Regex signal patterns and fuzzy app-catalog gating |
| `scanner/score/app_scorer.py` | Ranking formula for discontinued candidates |
| `scanner/cli.py`, `scanner/__main__.py` | Typer CLI |
| `scanner/config.py`, `scanner/logging.py`, `scanner/models.py` | Config loader, structlog setup, dataclasses |
| `migrations/*.sql` | Schema: sources, cursors, jobs, inbox, signals, apps, control, runs |
| `config.toml` | Runtime configuration |
| `known_apps.yaml` | Seed catalog of app names and aliases |
| `tests/` | 111 offline tests: parsers on snapshots, queue, rate limiter, DB, end-to-end orchestrator with a fake scraper |
| `pyproject.toml`, `requirements.txt`, `justfile` | Packaging, dependencies, task runner |
