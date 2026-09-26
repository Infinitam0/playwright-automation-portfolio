"""Jittered delay utilities, shared with my other scrapers."""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)


async def jittered_delay(
    base: float,
    jitter_factor: float = 0.5,
    minimum: float = 0.5,
) -> None:
    """Sleep base ± (base * jitter_factor), floored at `minimum`."""
    jitter_range = base * jitter_factor
    delay = max(base + random.uniform(-jitter_range, jitter_range), minimum)
    logger.debug(f"Jittered delay: {delay:.2f}s (base={base}, jitter={jitter_factor})")
    await asyncio.sleep(delay)
