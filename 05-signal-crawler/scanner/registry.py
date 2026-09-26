"""Scraper registry: decorator + convention loader.

Usage:
    @register_scraper("reddit")
    class RedditScraper(BaseScraper):
        ...

`load_all()` imports every module under `scanner.scrapers` so its decorators
fire, then returns a fresh snapshot of the registry. Drop a new file in
scanner/scrapers/ and you're done — no edits anywhere else.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scanner.base import BaseScraper


_REGISTRY: dict[str, type[BaseScraper]] = {}


def register_scraper(name: str):
    """Class decorator. Records the class under `name` in the global registry.

    Raises ValueError on duplicate registration to surface accidental conflicts
    (typo, copy-paste) loudly at import time.
    """
    if not name or not isinstance(name, str):
        raise ValueError(f"scraper name must be a non-empty string, got {name!r}")

    def deco(cls: type[BaseScraper]) -> type[BaseScraper]:
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            existing = _REGISTRY[name]
            raise ValueError(
                f"duplicate scraper registration: {name!r} "
                f"already maps to {existing.__module__}.{existing.__qualname__}"
            )
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def load_all() -> dict[str, type[BaseScraper]]:
    """Import every module under scanner.scrapers and return a registry snapshot."""
    pkg = importlib.import_module("scanner.scrapers")
    for module_info in pkgutil.iter_modules(pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"scanner.scrapers.{module_info.name}")
    return dict(_REGISTRY)


def get_registry() -> dict[str, type[BaseScraper]]:
    """Return the current registry snapshot without triggering imports."""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Test-only: empty the registry. Production code should never call this."""
    _REGISTRY.clear()
