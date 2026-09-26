"""Jittered delay utilities to randomize request timing."""

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
    """Sleep for a randomized duration around a base value.

    With default jitter_factor=0.5, a base of 2.0s produces delays
    uniformly distributed in [1.0, 3.0]. The minimum floor prevents
    zero or near-zero delays.
    """
    jitter_range = base * jitter_factor
    delay = base + random.uniform(-jitter_range, jitter_range)
    delay = max(delay, minimum)
    logger.debug(f"Jittered delay: {delay:.2f}s (base={base}, jitter={jitter_factor})")
    await asyncio.sleep(delay)
