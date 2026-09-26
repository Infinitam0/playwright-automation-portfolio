"""Support portal docs crawler — main entrypoint."""

import argparse
import asyncio
import pathlib

import auth
import extractor
import output
import scraper


def _ensure_dirs() -> None:
    """Create runtime directories if they do not exist."""
    for d in [".auth", "cache", "output", "state"]:
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Support portal docs crawler")
    p.add_argument("--headed", action="store_true", help="Show browser window")
    p.add_argument(
        "--slow",
        action="store_true",
        help="Add 500ms slow_mo delay (only effective with --headed)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Clear cache and visited log, re-scrape everything",
    )
    return p


def _print_run_summary(crawl_stats: dict, extraction_stats: dict, assembly_stats: dict) -> None:
    """Print structured run summary to stdout per DX-02."""
    word_count = assembly_stats.get("word_count", 0)
    output_files = assembly_stats.get("output_files", [])

    # Sum both failure categories: cache files that failed extraction AND
    # URLs that failed during crawl (404/timeout). Both reduce usable output.
    failed_count = (
        assembly_stats.get("failed_count", 0)
        + crawl_stats.get("failed", 0)
        + extraction_stats.get("failed", 0)
    )

    if len(output_files) == 1:
        output_str = output_files[0]
    else:
        output_str = f"output/ ({len(output_files)} files, split by category)"

    print(
        "\nRun complete\n"
        f"  Articles scraped:  {crawl_stats.get('scraped', 0)}\n"
        f"  Articles skipped:  {crawl_stats.get('skipped', 0)} (already cached)\n"
        f"  Articles failed:   {failed_count}\n"
        f"  Word count:        {word_count:,}\n"
        f"  Output:            {output_str}"
    )


async def run(args: argparse.Namespace) -> None:
    """Main async entry — BFS crawl + extraction pass + assembly."""
    _ensure_dirs()

    if args.force:
        output.force_reset(scraper.VISITED_PATH, scraper.CACHE_DIR, scraper.FRONTIER_PATH)
        print("--force: cleared visited log and cache. Starting full re-scrape.")

    username, password = auth.get_credentials()
    slow_mo = 500 if (args.headed and args.slow) else 0

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not args.headed,
            slow_mo=slow_mo,
        )
        try:
            context, page = await auth.login(browser, username, password)
            crawl_stats = await scraper.crawl(page, context, username, password, args)
            extraction_stats = await extractor.run_extraction_pass(page, context, username, password)
            assembly_stats = await output.assemble(
                scraper.CACHE_DIR,
                pathlib.Path("output"),
                page,
                context,
                username,
                password,
            )
            _print_run_summary(crawl_stats, extraction_stats, assembly_stats)
        finally:
            await browser.close()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
