"""Regression tests for the lifecycle sweep.

Run from 03-listing-monitor/:
    pip install -r requirements.txt pytest && python -m pytest -q tests
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.lifecycle import SweepObservation, run_lifecycle_sweep
from src.scraper.parsers import STATUS_AVAILABLE, STATUS_SOLD, STATUS_UNDER_BID

NOW = datetime(2026, 9, 26, 6, 0, 0)  # noqa: DTZ001 — naive, like the sweep's datetime.now()


def _sweep(monkeypatch, active, observation):
    """Run the sweep against an in-memory sheet; return {row: written cells}."""
    from src.storage import sheets

    written = {}
    monkeypatch.setattr(sheets, "load_active_lifecycle", lambda ws: active)
    monkeypatch.setattr(
        sheets, "batch_update_lifecycle",
        lambda ws, updates: written.update(dict(updates)) or len(updates),
    )
    settings = SimpleNamespace(lifecycle_sweep_enabled=True)
    asyncio.run(run_lifecycle_sweep(None, observation, settings, now=NOW))
    return written


def _open(row, status=STATUS_AVAILABLE):
    return {"row": row, "status_current": status, "date_listed": "", "first_seen_at": "2026-09-01T00:00:00"}


def test_sold_view_under_offer_stays_open(monkeypatch):
    # The sold view's default filter also returns under-offer listings; that is
    # not a sale, so the row must move to under offer and stay open.
    obs = SweepObservation(complete=False, sold_status_by_id={"1": STATUS_UNDER_BID})
    written = _sweep(monkeypatch, {"1": _open(2)}, obs)
    status, _changed, closed, days, sold = written[2]
    assert status == STATUS_UNDER_BID
    assert (closed, days, sold) == ("", "", "")


def test_sold_view_under_offer_already_under_offer_is_a_noop(monkeypatch):
    obs = SweepObservation(complete=False, sold_status_by_id={"1": STATUS_UNDER_BID})
    assert _sweep(monkeypatch, {"1": _open(2, STATUS_UNDER_BID)}, obs) == {}


@pytest.mark.parametrize("prev", [STATUS_AVAILABLE, STATUS_UNDER_BID])
def test_sold_view_sale_closes_as_sold(monkeypatch, prev):
    obs = SweepObservation(complete=False, sold_status_by_id={"1": STATUS_SOLD})
    written = _sweep(monkeypatch, {"1": _open(2, prev)}, obs)
    status, _changed, closed, days, sold = written[2]
    assert status == STATUS_SOLD
    assert closed == NOW.isoformat(timespec="seconds")
    assert days == "25"
    assert sold == "TRUE"
