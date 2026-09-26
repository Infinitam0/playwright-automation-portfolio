"""Retry with exponential backoff and circuit breaker."""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max: float = 60.0
    backoff_jitter: float = 0.5


class CircuitState(enum.Enum):
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Blocking all requests
    HALF_OPEN = "half_open"  # Allowing one test request


class CircuitBreaker:
    """Circuit breaker to prevent hammering a failing service.

    CLOSED -> OPEN (after failure_threshold failures)
    OPEN -> HALF_OPEN (after recovery_timeout seconds)
    HALF_OPEN -> CLOSED (on success) or OPEN (on failure)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 120.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0

    @property
    def is_open(self) -> bool:
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker -> HALF_OPEN (recovery timeout elapsed)")
                return False
            return True
        return False

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker -> CLOSED (success in half-open)")
        self._failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.warning("Circuit breaker -> OPEN (failure in half-open)")
        elif self._failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                f"Circuit breaker -> OPEN "
                f"({self._failure_count} consecutive failures)"
            )


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open."""

    pass


def is_retryable_error(error: Exception) -> bool:
    """Classify whether an error is worth retrying.

    Retryable: timeouts, connection resets, 429, 5xx
    Not retryable: 404, JS evaluation errors, navigation to invalid URL
    """
    msg = str(error).lower()

    # Not retryable
    if "404" in msg or "not found" in msg:
        return False
    if "evaluation failed" in msg or "execution context" in msg:
        return False

    # Retryable patterns
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
    circuit_breaker: CircuitBreaker | None = None,
    on_retry: Callable[[int, Exception], Awaitable[None]] | None = None,
) -> Any:
    """Execute an async function with exponential backoff retry.

    Args:
        func: The async function to retry.
        config: Retry configuration.
        circuit_breaker: Optional circuit breaker instance.
        on_retry: Optional async callback(attempt, error) invoked before each retry.
            This is the integration point for proxy rotation.

    Returns:
        The result of the function call.

    Raises:
        CircuitOpenError: If the circuit breaker is open.
        The last exception if all retries are exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(config.max_retries + 1):
        # Check circuit breaker
        if circuit_breaker and circuit_breaker.is_open:
            raise CircuitOpenError(
                f"Circuit breaker is open, skipping request "
                f"(will recover after {circuit_breaker.recovery_timeout}s)"
            )

        try:
            result = await func()
            if circuit_breaker:
                circuit_breaker.record_success()
            return result
        except Exception as e:
            last_error = e

            if circuit_breaker:
                circuit_breaker.record_failure()

            if not is_retryable_error(e):
                logger.debug(f"Non-retryable error, not retrying: {e}")
                raise

            if attempt >= config.max_retries:
                logger.warning(
                    f"All {config.max_retries} retries exhausted. Last error: {e}"
                )
                raise

            # Calculate backoff with jitter
            backoff = min(
                config.backoff_base * (config.backoff_multiplier ** attempt),
                config.backoff_max,
            )
            jitter = backoff * config.backoff_jitter
            delay = backoff + random.uniform(-jitter, jitter)
            delay = max(delay, 0.1)

            logger.info(
                f"Attempt {attempt + 1}/{config.max_retries + 1} failed: {e}. "
                f"Retrying in {delay:.1f}s..."
            )

            # Call on_retry callback (e.g., to rotate proxy)
            if on_retry:
                await on_retry(attempt, e)

            await asyncio.sleep(delay)

    # Should not reach here, but just in case
    raise last_error  # type: ignore[misc]
