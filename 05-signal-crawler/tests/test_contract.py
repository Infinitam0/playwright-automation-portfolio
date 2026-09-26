"""Scraper-contract tests.

These run against every scraper registered under scanner.scrapers/ at the time
the test session starts. As new scrapers land in later commits they get
contract-tested automatically — no edits needed here.
"""

from __future__ import annotations

import inspect

import pytest

from scanner.base import BaseScraper, RateLimited, SelectorBroken
from scanner.models import RawItem
from scanner.registry import (
    _REGISTRY,
    clear_registry,
    get_registry,
    load_all,
    register_scraper,
)


@pytest.fixture
def clean_registry():
    """Save/restore the global registry around a test so register_scraper calls
    inside the test body do not leak."""
    saved = dict(_REGISTRY)
    clear_registry()
    try:
        yield _REGISTRY
    finally:
        clear_registry()
        _REGISTRY.update(saved)


# ---------- registry mechanics ----------


def test_register_scraper_records_class(clean_registry) -> None:
    @register_scraper("a")
    class A(BaseScraper):
        default_rate_per_min = 10

        async def run(self, since_cursor):
            yield RawItem(source="a", source_item_id="1", raw_content="x")

    assert clean_registry == {"a": A}
    assert A.name == "a"


def test_register_scraper_rejects_duplicates(clean_registry) -> None:
    @register_scraper("dup")
    class A(BaseScraper):
        default_rate_per_min = 10

        async def run(self, since_cursor):
            yield RawItem(source="dup", source_item_id="1", raw_content="x")

    with pytest.raises(ValueError, match="duplicate"):

        @register_scraper("dup")
        class B(BaseScraper):
            default_rate_per_min = 20

            async def run(self, since_cursor):
                yield RawItem(source="dup", source_item_id="2", raw_content="y")


def test_register_scraper_rejects_empty_name(clean_registry) -> None:
    with pytest.raises(ValueError):
        register_scraper("")


def test_load_all_returns_dict() -> None:
    loaded = load_all()
    assert isinstance(loaded, dict)
    # Snapshot must match what get_registry sees immediately after.
    assert loaded == get_registry()


# ---------- BaseScraper sanity ----------


def test_base_scraper_cannot_be_instantiated_without_run() -> None:
    with pytest.raises(TypeError):
        BaseScraper()  # type: ignore[abstract]


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimited(60.0)
    assert err.retry_after == 60.0


def test_selector_broken_is_exception() -> None:
    assert issubclass(SelectorBroken, Exception)


# ---------- contract every registered scraper must pass ----------


def _registered_scrapers():
    return list(load_all().items())


@pytest.mark.parametrize("name,cls", _registered_scrapers())
def test_registered_scraper_obeys_contract(name: str, cls: type[BaseScraper]) -> None:
    """Every scraper module that loads must declare the required surface."""
    assert cls.name == name
    assert isinstance(cls.default_rate_per_min, int)
    assert cls.default_rate_per_min > 0
    assert issubclass(cls, BaseScraper)
    # `run` must be an async generator function (returns AsyncIterator on call).
    assert inspect.isasyncgenfunction(cls.run), (
        f"{cls.__name__}.run must be `async def ... yield`, not a coroutine returning a value"
    )


# ---------- RawItem hash determinism ----------


def test_raw_item_hash_is_deterministic() -> None:
    a = RawItem(source="x", source_item_id="1", raw_content="same body")
    b = RawItem(source="x", source_item_id="2", raw_content="same body")
    assert a.hash == b.hash


def test_raw_item_hash_differs_on_content() -> None:
    a = RawItem(source="x", source_item_id="1", raw_content="body-a")
    b = RawItem(source="x", source_item_id="1", raw_content="body-b")
    assert a.hash != b.hash
