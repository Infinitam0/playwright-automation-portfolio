# Permission bot with automatic 2FA (Playwright + Python)

A production bot, anonymised. The portal, organisation, people and UI labels are replaced with placeholders; the automation logic is unchanged.

## Problem

Keeping user permissions in a SaaS admin portal in sync with a spreadsheet, a manual, click-heavy task. Each account has a deep tree of checkboxes, and the portal's UI is the only practical way to change them in bulk.

## How it works

1. Load credentials from `.env` / environment and fail fast (exit 1 plus a log line) if any are missing.
2. Read `permissions.xlsx`, validate the header, and skip incomplete or invalid rows while keeping the row numbers for error reporting.
3. Group the rows per account.
4. Launch headless Chromium and open the admin URL. If the portal redirects to the login form, fill in the username and password.
5. When the 2FA field appears, generate the current TOTP code with `pyotp` and submit it. No human is needed.
6. Open user management and expand the user-group tree.
7. For each account, select it, open the access tab, walk each `ParentPath` node by node (expanding collapsed nodes), then check or uncheck the target checkbox.
8. Save and confirm, but only if at least one action succeeded, then move on to the next account. A failed action or account is recorded, and the run continues.
9. Append one line to `permission_bot.log` (`timestamp | SUCCESS/FAILURE | N accounts, M actions, K failures`) and exit 0 or 1, so a scheduler can alert on failure.

`export_permissions.py` does the reverse. It logs in the same way, walks a user's whole permission tree recursively, and writes every node's state (checked, unchecked, indeterminate, no checkbox) to Excel. This gives you a baseline to build `permissions.xlsx` from.

## Playwright techniques

- Accessible-role locators (`get_by_role("treeitem", name=...)`) instead of brittle CSS paths. The tree is navigated by node name, not position.
- Idempotent actions: `safe_check`/`safe_uncheck` read `is_checked()` first, so a re-run changes nothing that is already correct.
- Tree expansion driven by `aria-expanded`. Collapsed ancestors are opened top-down with XPath `ancestor::li[@role='treeitem']`, and the bot waits for the first child to become visible, not for a fixed delay.
- Fallback checkbox resolution: `input[type=checkbox]`, then `role=checkbox`, then the tree item itself.
- Tri-state reading via `el.indeterminate` (`locator.evaluate`) in the exporter.
- Optional 2FA step: a short `wait_for(state="visible")` on the code field, and the bot continues if the field never appears.
- Popup handling (`page.expect_popup()`) for the admin window in the exporter.
- `scroll_into_view_if_needed()` for long user lists, and `networkidle` waits after navigation.
- Headless by default, with headed mode (`HEADLESS=false`) for debugging.

## API integration (OAuth2)

`explore_api.py` is the API-side counterpart. It uses the OAuth2 **client-credentials** grant with `requests`: the client ID comes from `API_CLIENT_ID`, and the client secret is combined with a one-time code (TOTP). It then:

- reads the `access_token` from the token response and uses it as a bearer token against `API_BASE_URL`,
- probes common Swagger/OpenAPI paths and falls back to parsing the Swagger UI HTML for the spec URL,
- lists all endpoints, filters them by keyword (users, permissions, roles, access), and smoke-tests the GET endpoints with the bearer token, printing only the status and the response shape (keys or item count), never record data or tokens,
- with `--browse`, opens the Swagger UI in Playwright with the bearer header pre-set, so you can explore manually.

## Scheduling (Docker + GitHub Actions cron / on-change)

- **Docker**: this uses the `mcr.microsoft.com/playwright/python` base image, so the browsers are pre-installed. `docker compose up --build` mounts `permissions.xlsx` read-only and `./logs` read-write, and reads credentials from `.env`.
- **GitHub Actions** (`.github/workflows/run-permissions.yml`; copy it to `.github/workflows/` at the root of the deployment repo, because workflows in a subfolder never run) runs in three cases:
  - on push when `permissions.xlsx` changes (edit the sheet, commit, and the portal follows),
  - on a weekday cron (`0 6 * * 1-5`) to correct drift,
  - on manual dispatch, with a `dry_run` input that only validates the sheet.

  Credentials come from repository secrets (`PORTAL_USER`, `PORTAL_PASS`, `PORTAL_TOTP_SECRET`, `PORTAL_BASE_URL`). Logs are uploaded as artifacts: kept 14 days on failure and 7 days on success.


## Controls

- **Review of sheet changes**: the sheet only reaches the portal through a push to `main` (the on-change trigger). With branch protection on `main`, every permission change is a reviewed pull request, and the git history is the audit trail.
- **Dry run**: a manual `workflow_dispatch` with `dry_run: true` validates the sheet's columns without logging in or changing anything.
- **Secrets**: the password and the TOTP seed live in CI secrets (locally in a git-ignored `.env`). They never appear in the repo, the image or the logs.
- **Personal data**: the export lists every account's permissions, and `permissions.xlsx` names real accounts. Treat both as confidential: keep them out of public repos and tickets, and delete old exports.
- **Safe re-runs**: checks and unchecks are idempotent. For an account where every action failed, nothing is saved and the editor is closed without saving.
- **Visible failures**: any failed row or account makes the run exit 1 (failing the workflow) and logs a `FAILURE` line. Run logs are kept as workflow artifacts.

## How AI was used

I built it with Claude Code as a pair programmer, using a spec-first workflow. The private source repo shows this:

- A `CLAUDE.md` (219 lines) holding the project context, conventions and run instructions for the agent.
- A `.planning/` directory (35 files): a codebase map, domain research, v1 requirements, a 3-phase roadmap (Excel validation, then tree navigation, then a data-driven loop), per-phase plans and a milestone audit. Commits follow the phase and plan IDs (`feat(02-01): ...`, `docs(phase-3): ...`).
- **22 of 45 commits** carry a `Co-Authored-By: Claude` trailer.

Neither `CLAUDE.md` nor `.planning/` is included here, because they contain portal-specific details.

## Build time

- 44 of 45 commits landed within 5 working days, spread over about 1.5 weeks. The 45th is a later catch-up commit.
- The v1.0 milestone (Excel-driven, tree-navigating bot) was reached on the second day of work.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                               # fill in PORTAL_USER / PORTAL_PASS / PORTAL_TOTP_SECRET / PORTAL_BASE_URL
# adapt labels.py to the target portal's UI labels and selectors
# create permissions.xlsx (columns below) next to permission_bot.py
python permission_bot.py
```

Other entry points:

```bash
cp export_config.example.yaml export_config.yaml && python export_permissions.py
python explore_api.py            # needs API_CLIENT_ID / API_CLIENT_SECRET / PORTAL_TOTP_SECRET (+ API_BASE_URL)
docker compose up --build
```

Tests run offline, with Playwright mocked and no portal or credentials needed:

```bash
pip install pytest && pytest
```

### `permissions.xlsx` format

Row 1 must contain exactly these headers:

| AccountName | ParentPath | CheckboxName | Action |
|---|---|---|---|
| Doe, J. | Departments > Unit A > Team 1 | Shared folder | check |
| Doe, J. | Departments > Unit A | Archive | uncheck |
| Roe, R. |  | RootPermission | check |

- `AccountName` is the user's display name in the user tree.
- `ParentPath` lists the tree nodes to walk, separated by ` > `. Leave it empty for a root-level item.
- `CheckboxName` is the tree item to toggle.
- `Action` is `check` or `uncheck` (case-insensitive).

The user-group nodes that `permission_bot.py` expands (`"Org A Users"` then `"Managers"`) are tenant-specific. Change them in `setup_browser_and_navigate()` to match your portal.

## Files

| File | Purpose |
|---|---|
| `permission_bot.py` | Main bot: login + TOTP 2FA, reads the Excel file, checks/unchecks tree items per account, writes the run log |
| `export_permissions.py` | Scraper: walks each user's permission tree recursively and exports the states to Excel |
| `export_config.example.yaml` | Template for the exporter's users and tree path |
| `labels.py` | All portal UI labels and selectors in one place (placeholders; adapt to the target portal) |
| `explore_api.py` | OAuth2 client-credentials login, Swagger discovery and endpoint smoke tests |
| `tests/` | 51 offline pytest tests (config, Excel validation, grouping, idempotent checkbox ops, save-only-on-success, TOTP, login flow, `main()` exit codes) |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore` | Container run with the Excel file and logs mounted |
| `.github/workflows/run-permissions.yml` | Scheduled and on-change runs with secrets |
| `.env.example` | All environment variables, with empty credentials |
| `requirements.txt`, `pytest.ini`, `ruff.toml` | Dependencies and tooling config |
