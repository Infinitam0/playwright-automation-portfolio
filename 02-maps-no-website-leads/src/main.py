"""CLI entry point.

    python -m src.main --query "barbershop" --location "Utrecht, NL" --radius-km 3
    python -m src.main --query "hair salon" --lat 52.09 --lng 5.12 --max-places 20
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.logging_conf import setup as setup_logging  # noqa: E402
from src.pipeline import run_area, run_multi_area, run_single_search  # noqa: E402
from src.scraper.errors import ChallengeDetected  # noqa: E402

logger = logging.getLogger("maps-leads")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="maps-leads",
        description="Find nearby businesses with no website and export them to .xlsx",
    )
    p.add_argument(
        "--query",
        required=True,
        nargs="+",
        help='what to search; several are allowed, e.g. --query "hair salon" "nail salon"',
    )
    p.add_argument("--location", help='place name to centre on, e.g. "Utrecht, NL"')
    p.add_argument("--lat", type=float, help="centre latitude (skips geocoding)")
    p.add_argument("--lng", type=float, help="centre longitude (skips geocoding)")
    p.add_argument(
        "--radius-km",
        type=float,
        help="sweep this radius with a tiled grid; omit for a single search",
    )
    p.add_argument("--cell-km", type=float, default=1.5, help="grid cell size (default 1.5)")
    p.add_argument("--zoom", type=int, default=14, help="map zoom for the search (default 14)")
    p.add_argument("--max-places", type=int, help="stop after N places (use for smoke tests)")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between detail loads")
    p.add_argument(
        "--out", type=Path, default=ROOT / "data" / "leads.xlsx", help="output .xlsx path"
    )
    p.add_argument(
        "--db", type=Path, default=ROOT / "data" / "places.db", help="resume database path"
    )
    p.add_argument(
        "--no-subdivide",
        action="store_true",
        help="do not split tiles that hit the 120-result cap. Much faster; with a "
             "high duplicate rate the base grid is already near-exhaustive.",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="re-queue places that previously failed to load, then run",
    )
    p.add_argument(
        "--builders-are-real",
        action="store_true",
        help="treat business.site pages as real websites (default: treat as leads)",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(
        logging.DEBUG if args.verbose else logging.INFO,
        log_file=ROOT / "logs" / "run.log",
    )

    lat, lng = args.lat, args.lng
    if lat is None or lng is None:
        if not args.location:
            logger.error("give either --location or both --lat and --lng")
            return 2

        from src.geo import geocode
        from src.store import Store

        # Reuse the centre this database was started with. Geocoding the same
        # place twice can return different coordinates, and since tiles are
        # labelled by coordinate that would relabel the whole grid and re-harvest
        # everything already done.
        with Store(args.db) as store:
            remembered = store.remembered_centre(args.location)
            if remembered:
                lat, lng = remembered
                logger.info("%s -> %.5f,%.5f (pinned from %s)", args.location, lat, lng, args.db.name)
            else:
                try:
                    lat, lng = geocode(args.location)
                except Exception as e:  # noqa: BLE001
                    logger.error("could not geocode %r: %s", args.location, e)
                    return 2
                store.remember_centre(args.location, lat, lng)
                logger.info("%s -> %.5f,%.5f (pinned for future runs)", args.location, lat, lng)

    if args.retry_failed:
        from src.store import Store

        with Store(args.db) as store:
            n = store.reset_failed()
        logger.info("re-queued %d previously failed places", n)

    common = dict(
        out_path=args.out,
        profile_dir=ROOT / "data" / "profile",
        delay=args.delay,
        builders_are_leads=not args.builders_are_real,
        max_places=args.max_places,
    )

    try:
        if args.radius_km and len(args.query) > 1:
            stats = await run_multi_area(
                args.query, lat, lng,
                radius_km=args.radius_km, cell_km=args.cell_km,
                zoom=args.zoom, db_path=args.db,
                subdivide_saturated=not args.no_subdivide,
                **{k: v for k, v in common.items() if k != "max_places"},
            )
        elif args.radius_km:
            stats = await run_area(
                args.query[0], lat, lng,
                radius_km=args.radius_km, cell_km=args.cell_km,
                zoom=args.zoom, db_path=args.db, **common,
            )
        else:
            stats = await run_single_search(
                args.query[0], lat=lat, lng=lng, zoom=args.zoom, **common
            )
    except ChallengeDetected as e:
        logger.error("Google is challenging us — stopping. %s", e)
        logger.error("Progress is saved; re-run later to resume. Do not hammer it.")
        return 3
    except KeyboardInterrupt:
        logger.warning("interrupted — progress is saved, re-run to resume")
        return 130

    logger.info("done: %s", stats.summary())
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
