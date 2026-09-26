# shared: SSO session reuse

`browser_session.py` reuses the operator's **own** signed-in browser session on the same machine, so the bot runs as the operator on portals they already use (for example behind company SSO). Use it only with your own account and with permission to automate the target portal.

| Function | What it does |
|---|---|
| `copy_browser_profile(user_data_dir, profile, channel)` | Copies the operator's Edge/Chrome profile (site storage and cookies) to a temp dir, so the real profile is never modified. An empty `user_data_dir` means the default dir for `channel`. It skips LevelDB `LOCK` files and files the running browser holds, and falls back to the SQLite backup API for the cookie database the browser keeps locked. |
| `launch_browser(temp_dir, headless, channel, timeout_ms)` | `launch_persistent_context(channel="msedge")` on the copy. |
| `sso_login(page, entry_url, sso_button_name, account_name, warmup_url)` | Clicks through a "Sign in with SSO" page and account picker if shown, then visits the app domain to establish its session. |
| `safe_goto(page, url)` | `page.goto` that retries on `net::ERR_ABORTED` (an SSO redirect interrupting the navigation). |
| `cleanup_profile(temp_dir)` | Removes the temp copy. |

The bots in `07-*`, `08-*` and `10-*` import this module. Each entrypoint adds the repo root to `sys.path`, so run them from anywhere with `python <folder>/main.py`.

Self-check: `python shared/test_browser_session.py`
