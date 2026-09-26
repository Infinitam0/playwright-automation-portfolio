"""Listing lifecycle tracking — detect when a listing leaves the market.

The new-listing scrape is forward-only: once a listing_id is known it is never
revisited, so the sheet never records when a listing sells or is withdrawn.
This module adds a best-effort "sweep" that, riding the full search pass the
scraper already makes each run, reconciles the set of currently-listed ids
against the listings the sheet still considers open, and records the end-of-life
event (status, timestamp, days-on-market) so we can measure how fast homes leave
the market per area.

It is gated behind ``settings.lifecycle_sweep_enabled`` and only ever closes
listings when the search pass observed the *entire* inventory
(``SweepObservation.complete``) — a partial pass must never be read as "these
listings disappeared". Nothing here is allowed to break the core scrape: the
sweep is wrapped in defensive error handling and always returns normally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.scraper.parsers import (
    SOLD_STATUSES,
    STATUS_AVAILABLE,
    STATUS_CLOSED_PRICE_CHANGE,
    STATUS_CLOSED_UNKNOWN,
    STATUS_CLOSED_WITHDRAWN,
    STATUS_SOLD,
    STATUS_UNDER_BID,
    normalize_status,
    parse_listed_date,
)

logger = logging.getLogger(__name__)

# Sentinel a detail re-check returns when the page no longer resolves.
RECHECK_GONE = "GONE"


@dataclass
class SweepObservation:
    """What the search pass observed, handed to the lifecycle sweep.

    Populated as a side effect of ``scrape_search_results`` so the sweep does
    not require a second crawl.
    """

    # Every valid (city-passing) listing_id seen anywhere in the pass.
    seen_active_ids: set[str] = field(default_factory=set)
    # listing_id -> raw status badge text read off the card (if the portal renders one).
    badge_by_id: dict[str, str] = field(default_factory=dict)
    # listing_id -> normalized status from the portal's sold view (definitive close).
    sold_status_by_id: dict[str, str] = field(default_factory=dict)
    # True only when the pass traversed the full inventory (ran out of results
    # rather than early-stopping or hitting the max_pages ceiling mid-inventory).
    complete: bool = False


@dataclass
class Transition:
    """Outcome of classifying one listing's lifecycle state this sweep."""

    new_status: str
    closed: bool
    sold: bool | None  # True/False when known, None when unknown


def classify_transition(
    prev_status: str,
    still_present: bool,
    badge_status: str | None = None,
    recheck_status: str | None = None,
) -> Transition:
    """Decide a listing's new lifecycle state from what we observed.

    Args:
        prev_status: the listing's stored ``status_current`` (normalized).
        still_present: whether its id appeared in this pass's seen set.
        badge_status: normalized status read off the card this pass, if any.
        recheck_status: normalized status from a detail re-check, ``RECHECK_GONE``
            if the page no longer resolves, or None if no re-check was done.
    """
    if still_present:
        effective = badge_status or prev_status or STATUS_AVAILABLE
        if effective in SOLD_STATUSES:
            return Transition(effective, closed=True, sold=True)
        if effective == STATUS_UNDER_BID:
            return Transition(STATUS_UNDER_BID, closed=False, sold=None)
        return Transition(STATUS_AVAILABLE, closed=False, sold=None)

    # Disappeared from a complete pass.
    if recheck_status is not None:
        if recheck_status == RECHECK_GONE:
            return Transition(STATUS_CLOSED_WITHDRAWN, closed=True, sold=False)
        if recheck_status in SOLD_STATUSES:
            return Transition(recheck_status, closed=True, sold=True)
        if recheck_status == STATUS_AVAILABLE:
            # Still available on its own page => it left our results because it
            # was repriced out of the configured price band, not sold.
            return Transition(STATUS_CLOSED_PRICE_CHANGE, closed=True, sold=False)
        return Transition(STATUS_CLOSED_UNKNOWN, closed=True, sold=None)

    # No re-check available. If we had already seen a sold
    # badge, its disappearance confirms the sale; otherwise we can't tell why.
    if prev_status in SOLD_STATUSES:
        return Transition(prev_status, closed=True, sold=True)
    return Transition(STATUS_CLOSED_UNKNOWN, closed=True, sold=None)


def compute_days_on_market(
    date_listed_raw: str,
    first_seen_at_iso: str,
    closed_at: date,
    today: date | None = None,
) -> tuple[int | None, bool]:
    """Days from when the listing went live to when it closed.

    Prefers the portal's own "date listed" value; falls back to our first-seen
    timestamp (flagged as estimated). Returns ``(days, start_estimated)`` with
    ``days`` None when no start date can be determined. Negative spans are
    clamped to 0 (clock skew / same-day).
    """
    start = parse_listed_date(date_listed_raw, today=today)
    estimated = False
    if start is None:
        start = _date_from_iso(first_seen_at_iso)
        estimated = True
    if start is None or closed_at is None:
        return None, estimated
    return max(0, (closed_at - start).days), estimated


def _date_from_iso(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


async def run_lifecycle_sweep(
    worksheet: Any,
    observation: SweepObservation | None,
    settings: Any,
    now: datetime | None = None,
) -> int:
    """Reconcile open listings against this pass and record closures.

    Best-effort: never raises. Returns the number of rows updated (0 if the
    sweep is disabled, the pass was incomplete, or nothing changed).

    Must run *after* any insert this run so the row numbers it reads from the
    sheet are current (inserts shift rows). It takes its own fresh snapshot.
    """
    try:
        # Imported lazily so the pure helpers above stay importable (and unit
        # testable) without the gspread dependency.
        from src.storage.sheets import (
            batch_update_lifecycle,
            load_active_lifecycle,
        )

        if not getattr(settings, "lifecycle_sweep_enabled", False):
            return 0
        if observation is None:
            return 0

        now = now or datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        now_date = now.date()

        active = load_active_lifecycle(worksheet)
        if not active:
            return 0

        # Two independent signals:
        #  - Sold view (sold_status_by_id): definitive, always usable.
        #  - Disappearance (absent from the active pass): only trustworthy when
        #    the active pass traversed the FULL inventory; otherwise listings on
        #    un-crawled pages look "gone". So it is gated on observation.complete.
        do_disappearance = observation.complete
        if do_disappearance:
            # Sanity net: only a small fraction of open listings can genuinely
            # leave the market in a day. If most are unaccounted for (neither in
            # the active pass nor the sold view), the pass was broken — skip
            # disappearance (sold-view closes still proceed).
            present = sum(
                1 for fid in active
                if fid in observation.seen_active_ids or fid in observation.sold_status_by_id
            )
            if len(active) >= 10 and present < 0.5 * len(active):
                logger.warning(
                    f"Disappearance reconciliation skipped: only {present}/{len(active)} "
                    "open listings seen (suspected truncated/failed crawl)"
                )
                do_disappearance = False

        if not do_disappearance and not observation.sold_status_by_id:
            logger.info(
                "Lifecycle sweep: no usable signal this run "
                "(incomplete active pass and empty sold view)"
            )
            return 0

        updates: list[tuple[int, list[str]]] = []
        closed_count = 0
        for listing_id, info in active.items():
            prev = normalize_status(info.get("status_current", "")) or STATUS_AVAILABLE

            # The portal's sold view is the definitive signal: if this open listing
            # appears there, it sold / went under offer — close it precisely.
            sold_status = observation.sold_status_by_id.get(listing_id)
            if sold_status:
                t = Transition(
                    normalize_status(sold_status) or STATUS_SOLD,
                    closed=True,
                    sold=True,
                )
            elif do_disappearance:
                still_present = listing_id in observation.seen_active_ids
                badge = normalize_status(observation.badge_by_id.get(listing_id, "")) or None
                t = classify_transition(prev, still_present, badge_status=badge)
            else:
                # Incomplete active pass — can't assess this listing's presence.
                continue

            # Skip rows with nothing to record (the overwhelming majority).
            if not t.closed and t.new_status == prev:
                continue

            if t.closed:
                days, _est = compute_days_on_market(
                    info.get("date_listed", ""),
                    info.get("first_seen_at", ""),
                    now_date,
                    today=now_date,
                )
                days_cell = str(days) if days is not None else ""
                sold_cell = "TRUE" if t.sold is True else ("FALSE" if t.sold is False else "")
                updates.append((info["row"], [
                    t.new_status,  # status_current
                    now_iso,       # status_changed_at
                    now_iso,       # closed_seen_at
                    days_cell,     # days_on_market
                    sold_cell,     # sold_flag
                ]))
                closed_count += 1
            else:
                # Non-terminal status change (e.g. available -> under offer).
                updates.append((info["row"], [
                    t.new_status,  # status_current
                    now_iso,       # status_changed_at
                    "",            # closed_seen_at (still open)
                    "",            # days_on_market
                    "",            # sold_flag
                ]))

        written = batch_update_lifecycle(worksheet, updates)
        logger.info(
            f"Lifecycle sweep: {len(active)} open, {written} updated "
            f"({closed_count} closed)"
        )
        return written

    except Exception as e:  # best-effort: never break the scrape
        logger.error(f"Lifecycle sweep failed (non-fatal): {e}", exc_info=True)
        return 0
