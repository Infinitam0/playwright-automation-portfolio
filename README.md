# Playwright automation portfolio

Ten browser-automation bots I built with AI coding agents (Claude Code), from 2025–2026. They scrape portals behind SSO and 2FA, fill forms from spreadsheets, crawl, export, and push data to Sheets, Telegram, SQLite, Excel or APIs.

> All code is **sanitized**. Portals, organisations and people are anonymised, hosts come from env vars (`https://portal.example.com`), and data, logs and credentials are not included. Where a selector would identify the target site, it is replaced by a clearly marked placeholder. The automation logic is unchanged.

## The bots

| # | Bot | Stack | What it shows | Build time |
|---|---|---|---|---|
| 01 | [Multi-step wizard form-filler](01-wizard-form-filler/) | Node, Playwright | Data-driven step list, fill/click by label and role, a screenshot per step, assertions on the result screen, console and network error capture, JSON report | minutes |
| 02 | [Maps "no website" lead finder](02-maps-no-website-leads/) | Python, Patchright | Grid-tiling a radius, splitting saturated tiles, dedupe before detail loads, parsing Maps' embedded JSON, SQLite resume, Excel export, 60 tests | ~2 h (1 h hands-on) |
| 03 | [Real-estate listing monitor](03-listing-monitor/) | Python, Playwright async | Paginated search plus detail pages, opt-in lifecycle tracking (new, sold or under offer, off the market), stealth patches with fingerprint and proxy rotation, backoff, **push to Google Sheets + Telegram alerts**, Docker + cron | 2 days (4.5 h hands-on) |
| 04 | [Maps lead pipeline](04-maps-lead-pipeline/) | Python, Patchright | Discover → crawl → enrich → score → LLM-drafted email → CSV. The website crawl respects robots.txt and rate limits per host. It never auto-sends | ~1 week (5.5 h hands-on) |
| 05 | [Signal crawler](05-signal-crawler/) | Python, Playwright | AlternativeTo, Reddit and HN scrapers behind a crash-safe, resumable job queue in SQLite, 111 tests | ~40 min |
| 06 | [Permission bot with auto-2FA](06-permission-bot-2fa/) | Python, Playwright sync | Excel-driven permission sync (checks and unchecks tree items per account), headless login with a **TOTP** code, **OAuth2 REST API client**, Docker + GitHub Actions cron, 51 tests | 5 working days (v1.0 on day 2) |
| 07 | [Template export + bulk re-upload](07-template-export-upload/) | Python, Playwright async | Reusing an SSO session, reading a virtualized grid through its JS API, per-item file downloads, bulk uploads with retries | n/a |
| 08 | [Resumable calendar scraper](08-calendar-scraper/) | Python, Playwright async | Date navigation, `wait_for_function`, popup extraction, incremental Excel writes, resume from the last date | n/a |
| 09 | [Authenticated portal docs crawler](09-portal-docs-crawler/) | Python, Playwright async | Login with a reused `storage_state`, re-login on expiry, a trace on failure, resumable crawl, HTML → Markdown, 51 tests | ~4 working days |
| 10 | [ITSM export + re-assign bot](10-itsm-export-assign/) | Python, Playwright async | Nested iframes, "load more" pagination, filtering to Excel, a companion bot that fills autocomplete dialogs | ~40 min (re-assign bot) |

[`shared/`](shared/) holds the SSO session-reuse helper used by 07, 08 and 10. It copies a signed-in browser profile, launches a persistent context and navigates with retries.

Build times come from my Claude Code prompt history, git commits and file timestamps. 07 and 08 were imported to git in a single commit, so there is no data for them.

## How I work with AI

See **[PROMPTING.md](PROMPTING.md)**. In short: record the flow with codegen or explore it live with the Playwright MCP server, plan in Claude Code's plan mode against a `CLAUDE.md` of project rules, build in thin slices, and let the agent prove every slice with a small real run plus tests. Each bot's README has a "How AI was used" section with evidence from its git history, such as the number of commits co-authored by Claude.

## Pushing to a CRM

Bots 03 and 04 push to external services (Google Sheets, Telegram, a CSV for CRM import), and 06 includes an OAuth2 REST client. I've also built a CLI for the **GoHighLevel V2 API** (contacts, opportunities, pipelines, workflows). Adding a "push to GoHighLevel" step to any of these scrapers means upserting a contact per row.

## Running a bot

Each folder has its own `README.md` and `requirements.txt` or `package.json`, plus a `.env.example` or `config.example.yaml` where the bot needs config. 07, 08 and 10 also import `../shared`, so run them from inside this repo. The typical setup is:

```bash
cd 09-portal-docs-crawler
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env   # fill in the portal URL and credentials
python main.py --help
```

MIT licensed.
