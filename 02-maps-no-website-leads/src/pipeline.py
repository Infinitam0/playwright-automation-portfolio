"""Orchestration: harvest -> detail -> classify -> export.

Concurrency is deliberately 1. One page, paced with jitter. Google tolerates this
indefinitely; parallel detail loads are what earns a block, and a block costs far
more time than the parallelism saves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .classify import classify
from .excel import write_workbook
from .geo import Tile, subdivide, tile_grid
from .models import Place, PlaceDetail
from .scraper import selectors as S
from .scraper.browser import create_browser_session
from .scraper.detail import BlobUnavailable, fetch_detail
from .scraper.errors import ChallengeDetected
from .scraper.feed import harvest
from .scraper.timing import jittered_delay
from .store import Store

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    harvested: int = 0
    detailed: int = 0
    failed: int = 0
    skipped_cached: int = 0

    def summary(self) -> str:
        return (
            f"harvested={self.harvested} detailed={self.detailed} "
            f"failed={self.failed} cached={self.skipped_cached}"
        )


async def detail_places(
    page,
    places: list[Place],
    *,
    delay: float = 3.0,
    builders_are_leads: bool = True,
    on_detail=None,
) -> tuple[list[PlaceDetail], RunStats]:
    """Phase 2 over a list of harvested places.

    A place that fails to yield a record is counted as a failure and dropped —
    never emitted as a no-website lead. A parse failure is not evidence about the
    business, and treating it as one would fabricate leads.
    """
    stats = RunStats(harvested=len(places))
    details: list[PlaceDetail] = []

    for i, place in enumerate(places, 1):
        try:
            detail = await fetch_detail(page, place.maps_url, place.fid)
        except ChallengeDetected:
            raise  # caller stops the run; never retry into a ban
        except (BlobUnavailable, Exception) as e:  # noqa: BLE001
            logger.warning("[%d/%d] %s failed: %s", i, len(places), place.name, e)
            stats.failed += 1
            if on_detail:
                on_detail(place, None)
            await jittered_delay(delay, 0.4)
            continue

        detail.name = detail.name or place.name
        detail.website_tier = classify(detail.website, builders_are_leads=builders_are_leads)
        details.append(detail)
        stats.detailed += 1

        logger.info(
            "[%d/%d] %s -> %s%s",
            i,
            len(places),
            detail.name,
            detail.website_tier,
            f" ({detail.website})" if detail.website else "",
        )
        if on_detail:
            on_detail(place, detail)

        await jittered_delay(delay, 0.4)

    return details, stats


async def run_single_search(
    query: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
    zoom: int = 14,
    max_places: int | None = None,
    out_path: Path,
    profile_dir: Path,
    delay: float = 3.0,
    builders_are_leads: bool = True,
) -> RunStats:
    """One search, end to end. No grid, no database — the smallest useful run."""
    async with create_browser_session(profile_dir) as context:
        page = await context.new_page()

        places = await harvest(page, query, lat=lat, lng=lng, zoom=zoom, tile="single")
        if max_places:
            places = places[:max_places]
        if not places:
            logger.warning("nothing harvested for %r", query)
            return RunStats()

        details, stats = await detail_places(
            page, places, delay=delay, builders_are_leads=builders_are_leads
        )

    write_workbook(details, out_path)
    return stats


async def run_multi_area(
    queries: list[str],
    lat: float,
    lng: float,
    *,
    radius_km: float,
    cell_km: float = 2.0,
    zoom: int = 15,
    out_path: Path,
    db_path: Path,
    profile_dir: Path,
    delay: float = 2.5,
    builders_are_leads: bool = True,
    export_every: int = 50,
    subdivide_saturated: bool = True,
) -> RunStats:
    """Sweep an area for several business categories into one database.

    Phase 1 runs for every query first, then a single phase 2 details whatever is
    left pending. That ordering matters: a cafe matches both "cafe" and
    "restaurant", and deduping across all queries before detailing means each
    business costs exactly one page load no matter how many queries found it.

    The workbook is rewritten every `export_every` businesses and again on the way
    out, including when a challenge stops the run. A sweep this size takes hours;
    exporting only at the end would mean a block at hour five produced nothing.
    """
    stats = RunStats()

    with Store(db_path) as store:
        def export() -> None:
            write_workbook(store.all_details(), out_path)

        try:
            async with create_browser_session(profile_dir) as context:
                page = await context.new_page()

                base_tiles = tile_grid(lat, lng, radius_km, cell_km, zoom)
                logger.info(
                    "sweep: %d queries x %d tiles, harvesting and detailing "
                    "one category at a time",
                    len(queries), len(base_tiles),
                )

                seen = 0

                def persist(place: Place, detail: PlaceDetail | None) -> None:
                    nonlocal seen
                    if detail is None:
                        store.mark_failed(place.fid)
                    else:
                        store.save_detail(detail)
                    seen += 1
                    if seen % export_every == 0:
                        export()

                # Harvest AND detail each category before moving to the next, so
                # usable leads land in the workbook within the first category
                # rather than after every category has been harvested. Dedupe is
                # unaffected: a business found again by a later query is ignored
                # on insert and stays 'done', so it is still detailed only once.
                for qi, query in enumerate(queries, 1):
                    queue: list[Tile] = list(base_tiles)
                    found_here = 0
                    skipped_saturated = 0

                    while queue:
                        tile = queue.pop(0)
                        if store.tile_done(tile.label, query):
                            continue

                        places = await harvest(
                            page, query, lat=tile.lat, lng=tile.lng,
                            zoom=tile.zoom, tile=tile.label,
                        )
                        new = store.add_places(places)
                        saturated = len(places) >= S.SUBDIVIDE_THRESHOLD
                        store.record_tile(tile.label, query, len(places), saturated)
                        stats.harvested += new
                        found_here += new

                        if saturated:
                            if subdivide_saturated:
                                queue.extend(subdivide(tile))
                            else:
                                skipped_saturated += 1

                        await jittered_delay(delay, 0.4)

                    # Never let a coverage cap be silent — a run that skipped
                    # saturated tiles must say so, or the output reads as
                    # exhaustive when it is not.
                    if skipped_saturated:
                        logger.info(
                            "%r: %d tiles hit the 120 cap and were NOT subdivided "
                            "(--no-subdivide); some businesses in those areas are "
                            "not in this run",
                            query, skipped_saturated,
                        )

                    pending = store.pending_places()
                    logger.info(
                        "[%d/%d] %r harvested %d new; detailing %d pending",
                        qi, len(queries), query, found_here, len(pending),
                    )

                    _, phase2 = await detail_places(
                        page, pending, delay=delay,
                        builders_are_leads=builders_are_leads, on_detail=persist,
                    )
                    stats.detailed += phase2.detailed
                    stats.failed += phase2.failed
                    export()

                    tiers = store.tier_counts()
                    logger.info(
                        "[%d/%d] %r done. running totals: %d detailed, leads so far: "
                        "none=%d social=%d ordering=%d",
                        qi, len(queries), query, stats.detailed,
                        tiers.get("none", 0), tiers.get("social_only", 0),
                        tiers.get("ordering_platform", 0),
                    )
        finally:
            # Always leave a usable workbook behind, however the run ended.
            export()

    return stats


async def run_area(
    query: str,
    lat: float,
    lng: float,
    *,
    radius_km: float,
    cell_km: float = 1.5,
    zoom: int = 15,
    out_path: Path,
    db_path: Path,
    profile_dir: Path,
    delay: float = 3.0,
    builders_are_leads: bool = True,
    max_places: int | None = None,
) -> RunStats:
    """Full area sweep: tile the radius, harvest every tile, then detail the lot.

    Both phases are resumable. Phase 1 skips tiles already recorded, phase 2 works
    from whatever is still pending, so re-running after any interruption picks up
    where it stopped rather than re-hitting Google for what it already has.
    """
    stats = RunStats()

    with Store(db_path) as store:
        async with create_browser_session(profile_dir) as context:
            page = await context.new_page()

            # --- phase 1: harvest every tile, subdividing saturated ones -----
            queue: list[Tile] = tile_grid(lat, lng, radius_km, cell_km, zoom)
            done_tiles = 0

            while queue:
                tile = queue.pop(0)
                if store.tile_done(tile.label, query):
                    logger.debug("tile %s already harvested, skipping", tile.label)
                    continue

                places = await harvest(
                    page, query, lat=tile.lat, lng=tile.lng,
                    zoom=tile.zoom, tile=tile.label,
                )
                new = store.add_places(places)
                saturated = len(places) >= S.SUBDIVIDE_THRESHOLD
                store.record_tile(tile.label, query, len(places), saturated)
                done_tiles += 1
                stats.harvested += new

                # At the cap means the tile had more to give than Maps would show.
                if saturated:
                    children = subdivide(tile)
                    if children:
                        logger.info(
                            "tile %s saturated (%d) -> subdividing into %d",
                            tile.label, len(places), len(children),
                        )
                        queue.extend(children)

                await jittered_delay(delay, 0.4)

            logger.info(
                "phase 1 done: %d tiles, %d new places (%s)",
                done_tiles, stats.harvested, store.counts(),
            )

            # --- phase 2: detail everything still pending --------------------
            pending = store.pending_places(limit=max_places)
            logger.info("phase 2: %d places to detail", len(pending))

            def persist(place: Place, detail: PlaceDetail | None) -> None:
                if detail is None:
                    store.mark_failed(place.fid)
                else:
                    store.save_detail(detail)

            _, phase2 = await detail_places(
                page, pending, delay=delay,
                builders_are_leads=builders_are_leads, on_detail=persist,
            )
            stats.detailed = phase2.detailed
            stats.failed = phase2.failed

        # Export everything ever collected, not just this run's slice — that is
        # what makes a resumed run produce a complete file.
        write_workbook(store.all_details(), out_path)

    return stats
