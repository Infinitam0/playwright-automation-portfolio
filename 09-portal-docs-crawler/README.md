# 09 · Authenticated portal docs crawler (login → crawl → Markdown)

## Problem

A vendor's support and knowledge portal only shows its documentation after login. The goal is to export the articles an account can access as clean, structured Markdown for offline reference. The crawl runs for hours, so it has to reuse sessions, log in again when a session expires, stop and resume without losing progress, and go easy on the portal.

## How it works

1. `main.py` reads `PORTAL_USERNAME` / `PORTAL_PASSWORD` from `.env`. The portal root comes from `PORTAL_BASE_URL` and defaults to `https://support.example.com`.
2. **Auth** (`auth.py`): if `.auth/session.json` exists, it is loaded as Playwright `storage_state` and the session is checked by opening the portal home. If the session was redirected to a login URL, or no saved state exists, the login form is filled and `storage_state` is saved again. A Playwright trace is recorded during this initial login only and written to `auth_failure_trace.zip` only if that login fails. Re-logins later in the run are not traced.
3. **Crawl** (`scraper.py`): a breadth-first crawl starts from the portal home. Every link is canonicalised (query and fragment stripped), limited to the portal domain and deduplicated. Article URLs get a cache stub named `cache/<md5(url)>.md`.
4. After **every** page, `state/visited.json` and `state/frontier.json` (the BFS queue) are written atomically with a `.tmp` file plus `os.replace`. A crash or Ctrl+C resumes exactly where it stopped. `--force` wipes the state and starts over.
5. Each navigation is followed by a session check. An expired session triggers a re-login on the same page, followed by navigation back to the requested URL. If a re-login fails (the page is still on the login URL afterwards), the run aborts at once instead of looping. A random 1–2 s delay separates requests.
6. **Extract** (`extractor.py`): each pending or failed stub is fetched again, `<img>`/`<iframe>` are removed, and the main content is extracted with **trafilatura** as Markdown. If that result is empty or under 200 characters, **markdownify** converts the `<article>` element instead. Headings are demoted one level, UI artifacts are dropped, and a title/source/date header is prepended. When both extractors produce nothing, the stub gets a failure marker and is retried on the next run.
7. **Assemble** (`output.py`): category pages are visited to map articles to categories. The Markdown is grouped by category, sorted, and given a table of contents with GFM anchors, then written atomically to `output/portal_docs.md`. If the result goes over 350k words, it is split into one file per category.
8. A run summary prints articles scraped, skipped, failed, the word count and the output paths.

## Playwright techniques

- **Session persistence** with `browser.new_context(storage_state=...)` and `context.storage_state(path=...)`, so a normal run logs in once.
- **Session-expiry detection** by checking the post-navigation URL for login paths, with **re-login in place** on the same page object, a return to the requested URL, and an abort on the first failed re-login.
- **Tracing on failure only**, for the initial login: `context.tracing.start(screenshots=True, snapshots=True)`. The trace is thrown away on success and saved to a zip when that login fails, for debugging in the Playwright trace viewer.
- **Role-based locators** for the login form (`get_by_role("textbox", name=...)`), originally found with Playwright codegen.
- `wait_for_load_state("networkidle")`, then a short `wait_for_selector("a[href]")` for single-page-app pages that render from cache after the network goes idle.
- **Link harvesting through `page.evaluate`** of `a.href` rather than `getAttribute`, so the browser resolves relative URLs.
- Per-URL error classification (timeout, 404, navigation error). A failed page is recorded and the crawl continues.
- `--headed` and `--slow` (`slow_mo=500`) flags for watching a run while debugging.

## How AI was used

- The project was built with Claude Code, using a GSD-style `.planning/` workflow (not included). That covered project and requirements docs, a roadmap, and six phases with research, plans and summaries: foundation and auth, crawl loop and state, extraction, output, integration bug fixes, and tech-debt cleanup. A v1.0 milestone audit came at the end.
- Bug fixes went test-first: the commit history has paired `test(...)` RED / `feat(...)`/`fix(...)` GREEN commits.
- A `CLAUDE.md` project file (not included) was present, and Playwright MCP console logs show the portal was explored interactively before selectors were written.
- **39 of 106 commits** carry a `Co-Authored-By: Claude` trailer.

## Build time

About **4 working days**: 105 of the 106 commits fall within four consecutive days. One final commit came about three weeks later.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env               # set PORTAL_BASE_URL, PORTAL_USERNAME, PORTAL_PASSWORD
python main.py                     # headless crawl + extract + assemble
python main.py --headed --slow     # watch it work
python main.py --force             # discard state/cache and re-crawl

pytest -q                          # offline test suite (fakes instead of a browser)
```

Use only on portals you are licensed to access and in line with their terms.

Set the portal's URL layout with `PORTAL_LOGIN_PATH`, `PORTAL_HOME_PATH`, `PORTAL_ARTICLE_PATH` and `PORTAL_CATEGORY_PATH` (defaults `/login`, `/home`, `/articles/`, `/categories/`). Adjust the login-field labels in `auth.py` if needed. Set `PORTAL_TITLE_SUFFIX` to the site name the portal appends to page titles.

## Files

| Path | Purpose |
|---|---|
| `main.py` | CLI entrypoint (`--headed`, `--slow`, `--force`), runs crawl, extract, assemble, then prints a summary |
| `auth.py` | Credentials from env, login, `storage_state` reuse, expiry detection, re-login, failure trace |
| `scraper.py` | Resumable BFS crawl, atomic visited/frontier state, URL canonicalisation, cache stubs |
| `extractor.py` | trafilatura extraction with markdownify fallback, heading demotion, title cleanup, retry markers |
| `output.py` | Category mapping, TOC, atomic Markdown output, size-based split, `--force` reset |
| `validate_extraction.py` | One-off quality check on a seeded sample of live articles (failure rate, tables, nav leakage) |
| `tests/` | 51 offline tests with fake page and context objects and synthetic HTML fixtures |
| `.env.example` | Required environment variables (empty values) |
| `requirements.txt`, `pytest.ini`, `ruff.toml`, `.gitignore` | Dependencies, test config, lint config, ignores for state, cache, output and secrets |
