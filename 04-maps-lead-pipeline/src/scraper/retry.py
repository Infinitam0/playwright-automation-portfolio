"""Retry with exponential backoff + jitter, and a retryable-error classifier.

Used by the HTTP client; `ChallengeDetected` is also raised by the browser
scraper. The BAN_MARKERS list is tuned for polite crawling of arbitrary company
sites: a challenge / 403 / rate-block page is treated as HARD non-retryable so
we stop instead of hammering a host that is already pushing back.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max: float = 60.0
    backoff_jitter: float = 0.5


class ChallengeDetected(Exception):
    """A host served a challenge / block page. Never retry; give up on this target."""

    pass


# If ANY marker appears in an error message, treat as hard non-retryable.
BAN_MARKERS = (
    "403",
    "forbidden",
    "access denied",
    "cloudflare",
    "cf-ray",
    "turnstile",
    "blocked",
    "captcha",
    "verify you are human",
    "unusual activity",
    "enable javascript and cookies",
    "robots.txt disallow",
)


def is_retryable_error(error: Exception) -> bool:
    if isinstance(error, ChallengeDetected):
        return False

    msg = str(error).lower()

    if any(m in msg for m in BAN_MARKERS):
        return False
    if "404" in msg or "not found" in msg:
        return False
    if "evaluation failed" in msg or "execution context" in msg:
        return False

    retryable_patterns = [
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "connection closed",
        "net::err_",
        "429",
        "too many requests",
        "500",
        "502",
        "503",
        "504",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    ]
    return any(pattern in msg for pattern in retryable_patterns)


async def retry_with_backoff(
    func: Callable[[], Awaitable[Any]],
    config: RetryConfig,
    on_retry: Optional[Callable[[int, Exception], Awaitable[None]]] = None,
) -> Any:
    last_error: Optional[Exception] = None

    for attempt in range(config.max_retries + 1):
        try:
            return await func()
        except Exception as e:
            last_error = e

            if not is_retryable_error(e):
                logger.debug(f"Non-retryable error, not retrying: {e}")
                raise

            if attempt >= config.max_retries:
                logger.warning(
                    f"All {config.max_retries} retries exhausted. Last error: {e}"
                )
                raise

            backoff = min(
                config.backoff_base * (config.backoff_multiplier**attempt),
                config.backoff_max,
            )
            jitter = backoff * config.backoff_jitter
            delay = max(backoff + random.uniform(-jitter, jitter), 0.1)

            logger.info(
                f"Attempt {attempt + 1}/{config.max_retries + 1} failed: {e}. "
                f"Retrying in {delay:.1f}s..."
            )

            if on_retry:
                await on_retry(attempt, e)

            await asyncio.sleep(delay)

    raise last_error  # type: ignore[misc]
