"""Telegram notifications via Bot API.

Sends property listing alerts to configured chat IDs when new listings are found.
Uses Telegram Bot API with retry logic and best-effort delivery.
"""

import asyncio
import logging
from typing import Any

import httpx

from src.scraper import site_profile as P
from src.scraper.timing import jittered_delay

logger = logging.getLogger(__name__)

# Telegram sendMessage allows up to 4096 characters "after entities parsing".
# We budget on the raw string length with margin so we never hit
# "400 Bad Request: message is too long" — splitting into multiple messages instead.
MAX_MESSAGE_CHARS = 3800
MESSAGE_RESERVE_CHARS = 200  # reserved for header + footer per message
MESSAGE_DELAY_SECONDS = 1.0  # pacing between messages to one chat (Telegram: <=1 msg/s/chat)


async def send_telegram_notifications(listings: list[Any], settings: Any) -> None:
    """Send Telegram notifications to all configured chat IDs.

    Public API called from main.py after successful scrape with new listings.
    Sends to all chat IDs concurrently and logs summary.

    Args:
        listings: List of FullListing objects to notify about
        settings: Settings object with Telegram configuration

    Note:
        Never raises exceptions - best-effort delivery only.
        Logs all errors but allows scraper to complete successfully.
    """
    if not settings.telegram_enabled:
        return

    if not settings.telegram_chat_ids:
        logger.warning("Telegram enabled but no chat IDs configured")
        return

    if not settings.telegram_bot_token:
        logger.warning("Telegram enabled but bot token missing — skipping notifications")
        return

    try:
        messages = _build_messages(listings, settings)

        logger.info(
            f"Sending {len(messages)} Telegram message(s) to "
            f"{len(settings.telegram_chat_ids)} chat(s)..."
        )

        async def _send_all_to_chat(chat_id: str) -> bool:
            """Send every message part to one chat, sequentially and paced."""
            ok = True
            for i, message in enumerate(messages):
                sent = await _send_message(chat_id, message, settings)
                ok = ok and sent
                # Pace messages to respect Telegram's ~1 msg/s per-chat limit.
                if i < len(messages) - 1:
                    await jittered_delay(MESSAGE_DELAY_SECONDS, jitter_factor=0.3)
            return ok

        tasks = [_send_all_to_chat(chat_id) for chat_id in settings.telegram_chat_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = sum(1 for r in results if r is True)
        total = len(results)
        logger.info(f"Telegram notifications: {successes}/{total} chat(s) fully delivered")

        for i, result in enumerate(results):
            if result is not True:
                chat_id = settings.telegram_chat_ids[i]
                error = result if isinstance(result, Exception) else "one or more messages failed"
                logger.error(f"Failed to send Telegram to {chat_id}: {error}")

    except Exception as e:
        logger.error(f"Telegram notification failed: {e}", exc_info=True)


async def send_telegram_alert(text: str, settings: Any) -> bool:
    """Send a plain operational alert to all configured chat IDs.

    Public API for scraper health alerts (e.g. the portal blocking us). Unlike
    send_telegram_notifications this takes ready-made text, not listings.

    Args:
        text: HTML-formatted message text (parse_mode is HTML — escape it)
        settings: Settings object with Telegram configuration

    Returns:
        True if at least one chat received the message. Callers throttling
        repeat alerts must arm their cooldown on this, not on the attempt.

    Note:
        Never raises exceptions - best-effort delivery only. The caller is
        expected to also fail the run, so a Telegram outage cannot mute an alert.
    """
    if not settings.telegram_enabled:
        return False

    if not settings.telegram_chat_ids:
        logger.warning("Telegram enabled but no chat IDs configured")
        return False

    if not settings.telegram_bot_token:
        logger.warning("Telegram enabled but bot token missing — skipping alert")
        return False

    try:
        tasks = [
            _send_message(chat_id, text, settings)
            for chat_id in settings.telegram_chat_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = sum(1 for r in results if r is True)
        total = len(results)
        logger.info(f"Telegram alert: {successes}/{total} chat(s) delivered")

        if successes == 0:
            logger.error("Telegram alert reached NO chats — check bot token / chat IDs")

        return successes > 0

    except Exception as e:
        logger.error(f"Telegram alert failed: {e}", exc_info=True)
        return False


async def _send_message(chat_id: str, text: str, settings: Any) -> bool:
    """Send a single Telegram message via Bot API with retry logic.

    Args:
        chat_id: Telegram chat ID (user, group, or channel)
        text: HTML-formatted message text
        settings: Settings object with bot token

    Returns:
        True if sent successfully, False otherwise

    Note:
        Retries 3 times with exponential backoff (2s, 4s, 8s).
        Handles 429 rate limits (uses Telegram's retry_after), 4xx, and 5xx.
    """
    if settings.telegram_dry_run:
        logger.info(f"[DRY RUN] Would send Telegram to {chat_id}:\n{text}")
        return True

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)

                if response.status_code == 200:
                    logger.info(f"Telegram sent to {chat_id} (attempt {attempt}/{max_attempts})")
                    return True

                # Rate limit — Telegram provides retry_after in response
                if response.status_code == 429:
                    if attempt < max_attempts:
                        retry_after = 2 ** attempt
                        try:
                            body = response.json()
                            retry_after = body.get("parameters", {}).get("retry_after", retry_after)
                        except Exception:
                            pass
                        logger.warning(
                            f"Rate limited sending to {chat_id}, retrying in {retry_after}s "
                            f"(attempt {attempt}/{max_attempts})"
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        logger.error(f"Rate limit persisted for {chat_id} after {max_attempts} attempts")
                        return False

                # Client errors (4xx) — don't retry
                if 400 <= response.status_code < 500:
                    logger.error(
                        f"Client error sending to {chat_id}: {response.status_code} - {response.text}"
                    )
                    return False

                # Server errors (5xx) — retry
                if 500 <= response.status_code < 600:
                    if attempt < max_attempts:
                        backoff = 2 ** attempt
                        logger.warning(
                            f"Server error {response.status_code} for {chat_id}, retrying in {backoff}s "
                            f"(attempt {attempt}/{max_attempts})"
                        )
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        logger.error(
                            f"Server error persisted for {chat_id} after {max_attempts} attempts: "
                            f"{response.status_code}"
                        )
                        return False

                logger.error(
                    f"Unexpected status {response.status_code} sending to {chat_id}: {response.text}"
                )
                return False

        except httpx.TimeoutException:
            if attempt < max_attempts:
                backoff = 2 ** attempt
                logger.warning(
                    f"Timeout sending to {chat_id}, retrying in {backoff}s "
                    f"(attempt {attempt}/{max_attempts})"
                )
                await asyncio.sleep(backoff)
                continue
            else:
                logger.error(f"Timeout persisted for {chat_id} after {max_attempts} attempts")
                return False

        except Exception as e:
            logger.error(f"Error sending Telegram to {chat_id} (attempt {attempt}/{max_attempts}): {e}")
            if attempt < max_attempts:
                backoff = 2 ** attempt
                await asyncio.sleep(backoff)
                continue
            return False

    return False


def _format_listing_block(number: int, listing: Any) -> str:
    """Format a single listing as a numbered HTML block (no trailing blank line)."""
    summary = listing.summary
    detail = listing.detail if hasattr(listing, "detail") else None

    block = []

    # Title line
    parts = []
    if summary.property_type:
        parts.append(summary.property_type)
    if summary.street:
        parts.append(summary.street.replace("\xa0", " ").strip())
    if summary.price:
        parts.append(f"{P.CURRENCY_SYMBOL}{summary.price:,}")
    block.append(f"{number}. <b>{' | '.join(parts)}</b>")

    # Details line
    details_parts = []
    if summary.living_area_m2:
        details_parts.append(f"{summary.living_area_m2}m²")
    if summary.plot_area_m2:
        details_parts.append(f"Plot {summary.plot_area_m2}m²")
    if summary.bedrooms:
        details_parts.append(f"{summary.bedrooms} bed")
    if summary.rating:
        details_parts.append(f"Rating {summary.rating}")
    if detail and detail.build_year:
        details_parts.append(f"Built {detail.build_year}")
    if details_parts:
        block.append(f"   {' · '.join(details_parts)}")

    # Listing link
    if summary.url:
        block.append(f'   <a href="{summary.url}">View listing</a>')

    return "\n".join(block)


def _build_messages(listings: list[Any], settings: Any) -> list[str]:
    """Build one or more HTML messages so no listing is ever cut off.

    Listings are numbered continuously (1..N) and packed into as few messages as
    possible, each kept under MAX_MESSAGE_CHARS; a listing is never split across
    messages. When more than one message is needed, each header carries a (part/total)
    indicator. The Google Sheet link is appended to the last message only.

    Returns at least one message string.
    """
    count = len(listings)

    # Footer — appended to the last message only.
    footer = ""
    if settings.telegram_include_sheet_url and settings.spreadsheet_id:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.spreadsheet_id}"
        footer = f'📊 <a href="{sheet_url}">View all in Google Sheets</a>'

    # Greedily pack listing blocks into pages by character budget.
    budget = MAX_MESSAGE_CHARS - MESSAGE_RESERVE_CHARS
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for i, listing in enumerate(listings, start=1):
        block = _format_listing_block(i, listing)
        block_len = len(block) + 2  # account for the "\n\n" separator between blocks
        if current and current_len + block_len > budget:
            pages.append(current)
            current, current_len = [], 0
        current.append(block)
        current_len += block_len
    if current or not pages:
        pages.append(current)

    total_parts = len(pages)
    noun = "listing" if count == 1 else "listings"

    messages: list[str] = []
    for part, page in enumerate(pages, start=1):
        if total_parts == 1:
            header = f"🏠 <b>Found {count} new {noun}:</b>"
        else:
            header = f"🏠 <b>Found {count} new {noun} ({part}/{total_parts}):</b>"
        sections = [header, "", "\n\n".join(page)]
        if part == total_parts and footer:
            sections += ["", footer]
        messages.append("\n".join(sections))

    return messages
