# How I build bots with AI

I don't type the Playwright code line by line. I use Claude Code (and sometimes Codex) as the co-pilot. My job is to specify the bot, steer it and verify it. Below is the loop I use for every bot in this repo.

## 1. Capture the real flow first
- **Record it.** `playwright codegen <portal>` while I click through the task once by hand. The raw recording is not the bot. It is ground truth for the selectors and the order of steps, and I paste it into the prompt.
- **Or let the agent look for itself.** With the Playwright MCP server, Claude drives a real browser, reads the live DOM (iframes, virtualized grids, autocomplete dialogs) and reports what it finds before any code exists.

## 2. Plan before code
- Every project has a `CLAUDE.md` with project rules: stack, "credentials only from env", "never hardcode URLs", "screenshot on failure", logging format.
- For anything bigger than a one-file script, I start in **plan mode**. Claude writes a spec and a checklist in `tasks/todo.md` (inputs, outputs, failure modes, resume behaviour), and I approve or correct it before a line is written.
- A typical opening prompt:
  > "Here is a codegen recording of the flow and a sample of the input sheet. Build a Playwright (Python, sync) bot that does this for every row. Requirements: login from env incl. TOTP, `--dry-run` and `--limit N` flags, idempotent (skip rows already done), progress log so a crashed run resumes, screenshot plus DOM dump on any failure. Plan first; list the selectors you'll rely on and how you'll wait for each page state."

## 3. Build in thin vertical slices
- First slice: login, then one record end to end, run against the real portal in headed mode. Then pagination, retries and export.
- For bigger jobs I run **parallel subagents**, one per concern: the scraper, the exporter or API push, and the tests. That keeps the main context clean.

## 4. Make the AI prove it works
- I never accept "done" without a run. Claude runs the bot on a handful of records (a limit or a small input sheet, headed at first), reads the log and screenshots, and fixes what broke.
- Flaky selectors get fallback chains (`get_by_role` → label → CSS). Waits target a page state (a selector, a network idle, a `wait_for_function`). Fixed pauses are only used for politeness delays.
- Offline unit tests cover the parsers and the resume/dedupe logic, so later refactors don't break the parts a run can't see.

## 5. Keep a lessons file
When the agent makes a mistake I correct it once. Then it writes the rule into `tasks/lessons.md` or `CLAUDE.md`, for example "this portal's grid is virtualized, read rows through the grid API, not the DOM". The next AI coding agent's session starts with that knowledge.

## Why this is fast
The recording and the MCP exploration remove the guesswork about selectors. Plan mode removes rework. The small-run loop moves debugging onto the agent. A simple scraper or form-fill bot (login, loop over rows or pages, export or push) is usually a **same-day** job this way. Most of that time goes into verifying against the real portal, not writing code.
