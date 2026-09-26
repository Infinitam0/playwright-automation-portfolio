"""Proxy pool with health tracking and rotation strategies."""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class ProxyHealth(enum.Enum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    COOLDOWN = "cooldown"
    DEAD = "dead"


class RotationStrategy(enum.Enum):
    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    LEAST_USED = "least_used"


@dataclass
class ProxyEntry:
    """A proxy with health state and usage tracking."""

    url: str
    health: ProxyHealth = ProxyHealth.HEALTHY
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    cooldown_until: float = 0.0
    _cooldown_exponent: int = 0

    def to_playwright_proxy(self) -> dict:
        """Convert to Playwright's proxy config format."""
        parsed = urlparse(self.url)
        proxy: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy

    @property
    def is_available(self) -> bool:
        """Whether this proxy can be used right now."""
        if self.health == ProxyHealth.DEAD:
            return False
        if self.health == ProxyHealth.COOLDOWN:
            return time.monotonic() >= self.cooldown_until
        return True

    @property
    def display_name(self) -> str:
        """Proxy URL with credentials masked."""
        parsed = urlparse(self.url)
        if parsed.username:
            return f"{parsed.scheme}://*:*@{parsed.hostname}:{parsed.port}"
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


class ProxyPool:
    """Provider-agnostic proxy pool with health tracking."""

    def __init__(
        self,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        max_consecutive_failures: int = 3,
        max_total_failures: int = 10,
        cooldown_base_seconds: float = 60.0,
    ) -> None:
        self._proxies: list[ProxyEntry] = []
        self._strategy = strategy
        self._max_consecutive = max_consecutive_failures
        self._max_total = max_total_failures
        self._cooldown_base = cooldown_base_seconds
        self._round_robin_idx = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._proxies)

    def load_from_list(self, urls: list[str]) -> int:
        """Load proxies from a list of URL strings. Returns count loaded."""
        count = 0
        for url in urls:
            url = url.strip()
            if not url or url.startswith("#"):
                continue
            self._proxies.append(ProxyEntry(url=url))
            count += 1
        logger.info(f"Loaded {count} proxies from list")
        return count

    def load_from_file(self, path: Path) -> int:
        """Load proxies from a file (one URL per line, # comments)."""
        if not path.exists():
            logger.warning(f"Proxy file not found: {path}")
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()
        return self.load_from_list(lines)

    async def load_from_url(self, url: str) -> int:
        """Fetch proxy list from a remote URL."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                lines = resp.text.splitlines()
                return self.load_from_list(lines)
        except Exception as e:
            logger.error(f"Failed to load proxies from {url}: {e}")
            return 0

    async def acquire(self) -> ProxyEntry | None:
        """Get the next available proxy using the configured strategy.

        Returns None when all proxies are exhausted.
        """
        async with self._lock:
            available = [p for p in self._proxies if p.is_available]
            if not available:
                return None

            if self._strategy == RotationStrategy.RANDOM:
                return random.choice(available)

            if self._strategy == RotationStrategy.LEAST_USED:
                return min(available, key=lambda p: p.total_successes + p.total_failures)

            # Round robin
            # Find the next available starting from current index
            for _ in range(len(self._proxies)):
                idx = self._round_robin_idx % len(self._proxies)
                self._round_robin_idx += 1
                proxy = self._proxies[idx]
                if proxy.is_available:
                    return proxy
            return None

    async def report_success(self, proxy: ProxyEntry) -> None:
        """Mark a proxy as having succeeded."""
        async with self._lock:
            proxy.consecutive_failures = 0
            proxy.total_successes += 1
            proxy._cooldown_exponent = 0
            if proxy.health != ProxyHealth.HEALTHY:
                logger.info(f"Proxy {proxy.display_name} recovered to HEALTHY")
            proxy.health = ProxyHealth.HEALTHY

    async def report_failure(self, proxy: ProxyEntry) -> None:
        """Mark a proxy as having failed. Escalates health state."""
        async with self._lock:
            proxy.consecutive_failures += 1
            proxy.total_failures += 1

            if proxy.total_failures >= self._max_total:
                proxy.health = ProxyHealth.DEAD
                logger.warning(
                    f"Proxy {proxy.display_name} -> DEAD "
                    f"({proxy.total_failures} total failures)"
                )
            elif proxy.consecutive_failures >= self._max_consecutive:
                proxy.health = ProxyHealth.COOLDOWN
                cooldown = min(
                    self._cooldown_base * (2 ** proxy._cooldown_exponent),
                    600.0,
                )
                proxy.cooldown_until = time.monotonic() + cooldown
                proxy._cooldown_exponent += 1
                logger.warning(
                    f"Proxy {proxy.display_name} -> COOLDOWN for {cooldown:.0f}s "
                    f"({proxy.consecutive_failures} consecutive failures)"
                )
            else:
                proxy.health = ProxyHealth.SUSPECT
                logger.info(
                    f"Proxy {proxy.display_name} -> SUSPECT "
                    f"({proxy.consecutive_failures} consecutive failures)"
                )

    def stats(self) -> dict:
        """Return pool health summary for logging."""
        by_health = {}
        for h in ProxyHealth:
            by_health[h.value] = sum(1 for p in self._proxies if p.health == h)
        return {
            "total": len(self._proxies),
            **by_health,
            "available": sum(1 for p in self._proxies if p.is_available),
        }
