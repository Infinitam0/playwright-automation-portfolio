"""Real-estate listing monitor - main orchestrator."""

from __future__ import annotations

import asyncio
import html
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Ensure project root is on sys.path so `src.` imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Settings
from src.lifecycle import SweepObservation, run_lifecycle_sweep
from src.models import FullListing
from src.notifications.telegram import (
    send_telegram_alert,
    send_telegram_notifications,
)
from src.reporting import generate_report
from src.scraper.browser import (
    create_browser,
    create_browser_session,
    dismiss_cookie_consent,
)
from src.scraper.detail_page import scrape_detail, scrape_details_batch
from src.scraper.proxy_pool import ProxyEntry, ProxyPool, RotationStrategy
from src.scraper.retry import (
    CircuitBreaker,
    CircuitOpenError,
    RetryConfig,
    retry_with_backoff,
)
from src.scraper.search_page import (
    BlockedError,
    scrape_search_results,
    scrape_sold_ids,
)
from src.scraper.timing import jittered_delay
from src.storage.sheets import (
    append_listings,
    ensure_headers,
    get_client,
    get_worksheet,
    load_known_ids,
)

# Logging setup
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Throttle state for block alerts. Lives in logs/ (already gitignored and
# created above) so no new infrastructure is needed. The container has no
# volume, so a redeploy re-arms alerting — at most one duplicate message.
BLOCK_ALERT_STATE_FILE = LOG_DIR / ".block_alert"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_DIR / f"monitor_{datetime.now():%Y%m%d_%H%M%S}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)


async def _init_proxy_pool(settings: Settings) -> ProxyPool | None:
    """Create and populate a proxy pool from configured source."""
    strategy = RotationStrategy(settings.proxy_rotation_strategy)
    pool = ProxyPool(
        strategy=strategy,
        max_consecutive_failures=settings.proxy_max_consecutive_failures,
        max_total_failures=settings.proxy_max_total_failures,
        cooldown_base_seconds=settings.proxy_cooldown_seconds,
    )

    if settings.proxy_source == "file":
        count = pool.load_from_file(settings.proxy_file_path)
    elif settings.proxy_source == "url":
        count = await pool.load_from_url(settings.proxy_url)
    elif settings.proxy_source == "inline":
        count = pool.load_from_list(settings.proxy_list)
    else:
        logger.error(f"Unknown proxy source: {settings.proxy_source}")
        return None

    if count == 0:
        logger.warning("No proxies loaded — proxy features disabled")
        return None

    logger.info(f"Proxy pool ready: {count} proxies, strategy={strategy.value}")
    return pool


async def _acquire_proxy_config(
    pool: ProxyPool | None,
    fallback_direct: bool,
) -> tuple[ProxyEntry | None, dict | None]:
    """Acquire a proxy from the pool and return (entry, playwright_config).

    Returns (None, None) when using direct connection.
    """
    if pool is None:
        return None, None

    entry = await pool.acquire()
    if entry:
        logger.info(f"Using proxy: {entry.display_name}")
        return entry, entry.to_playwright_proxy()

    if fallback_direct:
        logger.warning("All proxies exhausted — falling back to direct connection")
        return None, None

    logger.error("All proxies exhausted and fallback_direct=False")
    return None, None


def _block_alert_due(settings: Settings) -> bool:
    """True if a block alert is due. Fails open.

    A block persists across every run until it clears, so without this the
    operator gets 4 messages a day and mutes the bot. State problems must never
    be the reason an alert is missed — every failure path returns True.
    """
    try:
        last = datetime.fromisoformat(
            BLOCK_ALERT_STATE_FILE.read_text(encoding="utf-8").strip()
        )
    except FileNotFoundError:
        return True
    except Exception as e:
        logger.warning(f"Block alert state unreadable ({e}) — alerting anyway")
        return True

    now = datetime.now(UTC)
    return now - last >= timedelta(hours=settings.block_alert_cooldown_hours)


def _record_block_alert() -> None:
    """Arm the cooldown. Called only once delivery is confirmed."""
    try:
        BLOCK_ALERT_STATE_FILE.write_text(
            datetime.now(UTC).isoformat(), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Could not record block alert state: {e}")


def _clear_block_alert_state() -> None:
    """Re-arm block alerting after a healthy pass."""
    try:
        BLOCK_ALERT_STATE_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Could not clear block alert state: {e}")


async def _alert_block(error: BlockedError, settings: Settings) -> None:
    """Tell the operator the scrape aborted, at most once per cooldown."""
    logger.error(f"Scrape aborted: {error}")
    if not _block_alert_due(settings):
        logger.info(
            "Block alert suppressed — already alerted within "
            f"{settings.block_alert_cooldown_hours}h"
        )
        return

    # Text is our own literals plus the page title/status carried on the error.
    # Escaped because _send_message hardcodes parse_mode=HTML.
    delivered = await send_telegram_alert(
        "🚫 <b>Listing monitor: scrape aborted</b>\n\n"
        f"{html.escape(str(error))}\n\n"
        "The lifecycle sweep was skipped. Any listings the search pass had "
        "already collected were still written.\n"
        f"Next reminder in ~{settings.block_alert_cooldown_hours:.0f}h "
        "unless it recovers.",
        settings,
    )
    if delivered:
        _record_block_alert()
    else:
        logger.warning(
            "Block alert undelivered — leaving the cooldown unarmed so the next "
            "run alerts again rather than suppressing into silence"
        )


async def run() -> None:
    settings = Settings()
    logger.info("=== Listing monitor starting ===")
    logger.info(f"Areas: {settings.areas}, Max pages: {settings.max_pages}")
    logger.info(f"Headless: {settings.headless}, Details: {settings.scrape_details}")
    logger.info(
        f"Proxy: {settings.proxy_enabled}, Fingerprint: {settings.fingerprint_enabled}, "
        f"Jitter: {settings.delay_jitter_factor}"
    )
    if not (
        settings.telegram_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_ids
    ):
        logger.warning(
            "Block alerts have no delivery channel — set LISTING_TELEGRAM_* or a "
            "block will only be visible in these logs"
        )

    # 1. Connect to Google Sheets and load known IDs
    logger.info("Connecting to Google Sheets...")
    gs_client = get_client(settings)
    worksheet = get_worksheet(gs_client, settings)
    ensure_headers(worksheet)
    known_ids = load_known_ids(worksheet)

    # Auto-detect first run: if sheet is empty, use higher max pages
    is_backfill = len(known_ids) == 0
    if is_backfill:
        effective_max_pages = max(settings.max_pages, 50)
        logger.info(
            f"Empty sheet detected — backfill mode, "
            f"using max_pages={effective_max_pages}"
        )
        settings.max_pages = effective_max_pages

    # 2. Initialize proxy pool + retry infra (if enabled)
    proxy_pool: ProxyPool | None = None
    circuit_breaker: CircuitBreaker | None = None
    retry_config: RetryConfig | None = None

    if settings.proxy_enabled:
        proxy_pool = await _init_proxy_pool(settings)

    circuit_breaker = CircuitBreaker(
        failure_threshold=settings.circuit_failure_threshold,
        recovery_timeout=settings.circuit_recovery_seconds,
    )
    retry_config = RetryConfig(
        max_retries=settings.max_retries,
        backoff_base=settings.backoff_base_seconds,
        backoff_multiplier=settings.backoff_multiplier,
        backoff_max=settings.backoff_max_seconds,
    )

    # 3. Lifecycle sweep: run at most once per day (on the configured UTC hour).
    #    The observation is populated as a side effect of the search pass and
    #    consumed after writing, so listings that left the market are detected
    #    even on runs that find no new listings.
    do_sweep = (
        settings.lifecycle_sweep_enabled
        and datetime.now(UTC).hour == settings.lifecycle_sweep_hour_utc
    )
    observation: SweepObservation | None = SweepObservation() if do_sweep else None

    # 4. Decide which browser path to use
    use_session = settings.proxy_enabled or settings.fingerprint_enabled

    # A block raises rather than looking like "no new listings": alert, then let
    # it propagate so the run exits non-zero and supercronic records a failure.
    try:
        if use_session:
            await _run_with_session(
                settings, known_ids, proxy_pool, circuit_breaker, retry_config,
                worksheet, observation,
            )
        else:
            await _run_simple(settings, known_ids, worksheet, observation)
    except BlockedError as e:
        await _alert_block(e, settings)
        raise

    _clear_block_alert_state()

    # 5. Reconcile listing lifecycle (best-effort; never raises). Runs after the
    #    scrape path so any inserts have already shifted rows into place.
    if do_sweep:
        await run_lifecycle_sweep(worksheet, observation, settings)
        # Refresh the precomputed per-area summary tab for the dashboard
        # (best-effort; reuses the existing Sheets client).
        generate_report(settings, gs_client)


async def _run_simple(
    settings: Settings,
    known_ids: set[str],
    worksheet,
    observation: SweepObservation | None = None,
) -> None:
    """Original scraping flow — no proxy, no fingerprint rotation."""
    async with create_browser(settings) as page:
        # Navigate to first page and handle cookies
        logger.info("Loading first search page...")
        await page.goto(
            settings.search_url_page(1), wait_until="domcontentloaded"
        )
        await dismiss_cookie_consent(page)
        await asyncio.sleep(1)

        logger.info("Scraping search results...")
        new_listings = await scrape_search_results(
            page, settings, known_ids, observation
        )
        logger.info(f"Found {len(new_listings)} new listings from search")

        # Gather the precise sold signal while the page is still open (runs even
        # when there are no new listings — closures happen independently).
        sold_block: BlockedError | None = None
        if observation is not None and settings.sold_sweep_enabled:
            try:
                observation.sold_status_by_id = await scrape_sold_ids(page, settings)
            except BlockedError as e:
                # Hold the abort until the active pass's listings are written.
                # Raising here would discard them, and a sold view that stays
                # blocked would re-discard them on every subsequent run.
                sold_block = e
            except Exception as e:
                logger.error(f"Sold sweep failed (non-fatal): {e}")

        details = {}
        if not new_listings:
            logger.info("No new listings found. Done.")
        elif settings.scrape_details:
            urls = [listing.url for listing in new_listings]
            logger.info(f"Scraping {len(urls)} detail pages...")
            details = await scrape_details_batch(page, urls, settings)
            logger.info(f"Got details for {len(details)} listings")

    if new_listings:
        await _write_results(new_listings, details, settings, worksheet)
    if sold_block:
        raise sold_block


async def _run_with_session(
    settings: Settings,
    known_ids: set[str],
    proxy_pool: ProxyPool | None,
    circuit_breaker: CircuitBreaker,
    retry_config: RetryConfig,
    worksheet,
    observation: SweepObservation | None = None,
) -> None:
    """Scraping flow with proxy rotation, fingerprint randomization, and retry."""
    async with create_browser_session(settings) as session:
        # Acquire initial proxy
        proxy_entry, proxy_config = await _acquire_proxy_config(
            proxy_pool, settings.proxy_fallback_direct
        )

        # Create first page
        page = await session.new_page(proxy=proxy_config)

        # Mutable holder so retry callbacks can update the page reference
        page_holder: list = [page]

        async def on_retry(attempt: int, error: Exception) -> None:
            """Callback invoked before each retry — rotates proxy if available."""
            nonlocal proxy_entry, proxy_config
            if proxy_pool and proxy_entry:
                await proxy_pool.report_failure(proxy_entry)
            proxy_entry, proxy_config = await _acquire_proxy_config(
                proxy_pool, settings.proxy_fallback_direct
            )
            new_page = await session.rotate(proxy=proxy_config)
            page_holder[0] = new_page
            # Re-dismiss cookies on new context
            await dismiss_cookie_consent(page_holder[0])

        # Navigate to first page with retry
        logger.info("Loading first search page...")
        first_url = settings.search_url_page(1)

        try:
            await retry_with_backoff(
                func=lambda: page_holder[0].goto(
                    first_url, wait_until="domcontentloaded"
                ),
                config=retry_config,
                circuit_breaker=circuit_breaker,
                on_retry=on_retry,
            )
        except CircuitOpenError as e:
            # Was a silent exit-0 — the same shape as the block it now reports.
            raise BlockedError(
                f"UNREACHABLE: circuit breaker open loading the first search page ({e})"
            ) from e
        except Exception as e:
            raise BlockedError(
                f"UNREACHABLE: first search page never loaded after retries ({e})"
            ) from e

        if proxy_pool and proxy_entry:
            await proxy_pool.report_success(proxy_entry)

        await dismiss_cookie_consent(page_holder[0])
        await asyncio.sleep(1)

        # Scrape search results
        logger.info("Scraping search results...")
        new_listings = await scrape_search_results(
            page_holder[0], settings, known_ids, observation
        )
        logger.info(f"Found {len(new_listings)} new listings from search")

        # Gather the precise sold signal while the page is still open (runs even
        # when there are no new listings — closures happen independently).
        sold_block: BlockedError | None = None
        if observation is not None and settings.sold_sweep_enabled:
            try:
                observation.sold_status_by_id = await scrape_sold_ids(
                    page_holder[0], settings
                )
            except BlockedError as e:
                # Hold the abort until the active pass's listings are written.
                # Raising here would discard them, and a sold view that stays
                # blocked would re-discard them on every subsequent run.
                sold_block = e
            except Exception as e:
                logger.error(f"Sold sweep failed (non-fatal): {e}")

        # Scrape detail pages with per-URL retry
        details = {}
        if not new_listings:
            logger.info("No new listings found. Done.")
        elif settings.scrape_details:
            urls = [listing.url for listing in new_listings]
            logger.info(f"Scraping {len(urls)} detail pages...")
            details = await _scrape_details_with_retry(
                session=session,
                page_holder=page_holder,
                urls=urls,
                settings=settings,
                proxy_pool=proxy_pool,
                proxy_entry=proxy_entry,
                proxy_config=proxy_config,
                circuit_breaker=circuit_breaker,
                retry_config=retry_config,
            )
            logger.info(f"Got details for {len(details)} listings")

        _log_proxy_stats(proxy_pool)

    if new_listings:
        await _write_results(new_listings, details, settings, worksheet)
    if sold_block:
        raise sold_block


async def _scrape_details_with_retry(
    session,
    page_holder: list,
    urls: list[str],
    settings: Settings,
    proxy_pool: ProxyPool | None,
    proxy_entry: ProxyEntry | None,
    proxy_config: dict | None,
    circuit_breaker: CircuitBreaker,
    retry_config: RetryConfig,
) -> dict:
    """Scrape detail pages with per-URL retry and proxy rotation."""
    from src.models import ListingDetail

    results = {}
    total = len(urls)

    for i, url in enumerate(urls, 1):
        logger.info(f"  Detail {i}/{total}: {url}")

        async def on_detail_retry(attempt: int, error: Exception) -> None:
            nonlocal proxy_entry, proxy_config
            if proxy_pool and proxy_entry:
                await proxy_pool.report_failure(proxy_entry)
            proxy_entry, proxy_config = await _acquire_proxy_config(
                proxy_pool, settings.proxy_fallback_direct
            )
            new_page = await session.rotate(proxy=proxy_config)
            page_holder[0] = new_page
            await dismiss_cookie_consent(page_holder[0])

        try:
            detail = await retry_with_backoff(
                func=lambda url=url: scrape_detail(
                    page_holder[0], url, settings
                ),
                config=retry_config,
                circuit_breaker=circuit_breaker,
                on_retry=on_detail_retry,
            )
            results[url] = detail
            if proxy_pool and proxy_entry:
                await proxy_pool.report_success(proxy_entry)
        except CircuitOpenError:
            logger.warning(f"  Circuit open, skipping detail: {url}")
            results[url] = ListingDetail()
        except Exception as e:
            logger.error(f"  Failed to scrape detail {url}: {e}")
            results[url] = ListingDetail()

        if i < total:
            await jittered_delay(
                settings.detail_delay_seconds, settings.delay_jitter_factor
            )

    return results


async def _write_results(new_listings, details, settings, worksheet) -> None:
    """Build full listings and write to Google Sheets."""
    now = datetime.now()
    full_listings: list[FullListing] = []
    for summary in new_listings:
        detail = details.get(summary.url)
        full_listings.append(
            FullListing(summary=summary, detail=detail, scraped_at=now)
        )

    logger.info(f"Writing {len(full_listings)} listings to Google Sheets...")
    inserted = append_listings(worksheet, full_listings)
    logger.info(f"=== Done. Inserted {inserted} new listings ===")

    # Send Telegram notifications for new listings
    if inserted > 0 and settings.telegram_enabled:
        try:
            await send_telegram_notifications(full_listings[:inserted], settings)
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}", exc_info=True)
            # Continue - scrape succeeded even if notification failed


def _log_proxy_stats(proxy_pool: ProxyPool | None) -> None:
    """Log proxy pool statistics if proxy pool is active."""
    if proxy_pool:
        stats = proxy_pool.stats()
        logger.info(f"Proxy pool stats: {stats}")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
