"""One-off validation script — extract 15–20 live portal articles and inspect quality.

Run: python validate_extraction.py
Writes extracted markdown to cache/ (overwrites stubs for the sampled URLs).
Prints inline inspection report.
"""

import asyncio
import hashlib
import json
import pathlib
import random
import sys

import auth
import extractor

VISITED_PATH = pathlib.Path("state/visited.json")
CACHE_DIR = pathlib.Path("cache")

# Fixed seed for reproducible sample
SAMPLE_SEED = 42
SAMPLE_SIZE = 20


def url_to_cache_path(url: str) -> pathlib.Path:
    """Reproduce the hash-based cache filename used by scraper.py."""
    h = hashlib.md5(url.encode()).hexdigest()
    return CACHE_DIR / f"{h}.md"


def select_sample() -> list[str]:
    """Return a reproducible sample of 20 article URLs from visited.json."""
    with open(VISITED_PATH) as f:
        data = json.load(f)
    articles = [url for url, v in data.items() if v.get("is_article")]
    random.seed(SAMPLE_SEED)
    return random.sample(articles, min(SAMPLE_SIZE, len(articles)))


async def run() -> None:
    username, password = auth.get_credentials()

    from playwright.async_api import async_playwright

    sample_urls = select_sample()
    print(f"Validating {len(sample_urls)} articles...\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context, page = await auth.login(browser, username, password)

            results = []
            for i, url in enumerate(sample_urls, 1):
                cache_path = url_to_cache_path(url)

                try:
                    await page.goto(url)
                    await page.wait_for_load_state("networkidle")
                    await auth.ensure_authenticated(page, context, username, password, target_url=url)

                    html = await page.content()
                    title_raw = await page.title()
                    title = extractor.clean_title(title_raw)

                    body = extractor.extract(html, url)

                    import datetime

                    date_today = datetime.date.today().isoformat()
                    header = extractor.build_header(title, url, date_today)
                    full_content = header + body

                    cache_path.write_text(full_content, encoding="utf-8")

                    failed = "extraction failed" in body
                    has_table = "|" in body
                    has_nav = any(nav in body for nav in ["Sign in", "Articles", "Support", "Home\n"])
                    body_len = len(body)

                    status = "FAILED" if failed else "OK"
                    results.append(
                        {
                            "url": url,
                            "title": title,
                            "status": status,
                            "body_len": body_len,
                            "has_table": has_table,
                            "has_nav": has_nav,
                        }
                    )

                    table_flag = " [TABLE]" if has_table else ""
                    nav_flag = " [NAV-LEAK!]" if has_nav else ""
                    print(f"[{i}/{len(sample_urls)}] {status} ({body_len} chars){table_flag}{nav_flag}")
                    print(f"  Title: {title}")
                    print(f"  URL:   {url}")
                    print()

                except Exception as exc:
                    print(f"[{i}/{len(sample_urls)}] ERROR: {url} — {exc}", file=sys.stderr)
                    results.append(
                        {
                            "url": url,
                            "status": "ERROR",
                            "body_len": 0,
                            "has_table": False,
                            "has_nav": False,
                            "title": "",
                        }
                    )

                await asyncio.sleep(random.uniform(1.0, 2.0))

        finally:
            await browser.close()

    # Summary
    total = len(results)
    if total == 0:
        print("Nothing sampled — no cached pages to validate.")
        return
    ok = sum(1 for r in results if r["status"] == "OK")
    failed = sum(1 for r in results if r["status"] in ("FAILED", "ERROR"))
    tables = sum(1 for r in results if r["has_table"])
    nav_leaks = sum(1 for r in results if r["has_nav"])

    print("=" * 60)
    print("EXTRACTION VALIDATION SUMMARY")
    print("=" * 60)
    print(f"Total sampled:  {total}")
    print(f"Succeeded:      {ok}  ({ok / total * 100:.0f}%)")
    print(f"Failed/Error:   {failed}  ({failed / total * 100:.0f}%)")
    print(f"With tables:    {tables}")
    print(f"Nav leaks:      {nav_leaks}")
    print()
    if failed / total > 0.20:
        print("WARNING: failure rate exceeds 20% — investigate before sign-off.")
    else:
        print("Failure rate within acceptable threshold (<= 20%).")


if __name__ == "__main__":
    asyncio.run(run())
