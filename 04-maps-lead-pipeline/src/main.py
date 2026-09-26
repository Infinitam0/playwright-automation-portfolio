"""Maps lead pipeline — CLI.

Subcommands (each stage is independently runnable):
  discover  find candidate companies          (--vertical --region --source --limit)
  enrich    crawl sites for email/certs/phone (--limit)
  score     compute fit + priority tiers      (--vertical)
  draft     AI/template outreach emails       (--tier --limit)
  export    write the review CSV              (--out --tier)
  run       discover -> ... -> export         (all of the above flags)
  stats     summarise the database
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import db, pipeline  # noqa: E402
from src.config import Settings  # noqa: E402
from src.models import VERTICALS  # noqa: E402
from src.net.http import HttpClient  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_DIR / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("leads")


def _apply_log_level(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), None)
    if isinstance(level, int):
        logging.getLogger().setLevel(level)


async def _run_async(args, settings: Settings, conn) -> int:
    """Dispatch an async subcommand and return the process exit code.

    `discover` and `run` gate on a real result: producing zero candidates /
    zero exported rows is treated as a failure (exit 4) rather than a silent
    empty success, so a misconfigured API key surfaces loudly.
    """
    http = HttpClient(settings)
    try:
        if args.command == "discover":
            found = await pipeline.run_discover(
                conn, http, settings,
                vertical=args.vertical, region=args.region,
                source=args.source, limit=args.limit,
            )
            if found == 0:
                print(
                    "No rows exported — check GOOGLE_MAPS_API_KEY, --source and the --tier/--include-no-email filters",
                    file=sys.stderr,
                )
                return 4
        elif args.command == "enrich":
            await pipeline.run_enrich(conn, http, settings, limit=args.limit)
        elif args.command == "draft":
            await pipeline.run_draft(conn, settings, tier=args.tier, limit=args.limit)
        elif args.command == "run":
            exported = await pipeline.run_all(
                conn, http, settings,
                vertical=args.vertical, region=args.region, source=args.source,
                out=Path(args.out), limit=args.limit, tier=args.tier,
                include_no_email=args.include_no_email,
            )
            if exported == 0:
                print(
                    "No rows exported — check GOOGLE_MAPS_API_KEY, --source and the --tier/--include-no-email filters",
                    file=sys.stderr,
                )
                return 4
    finally:
        await http.aclose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Maps lead pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    verticals = ["all", *VERTICALS]
    sources = ["places", "maps"]

    p = sub.add_parser("discover", help="find candidate companies")
    p.add_argument("--vertical", choices=verticals, default="all")
    p.add_argument("--region", default="all", help="all | <province> | <city>")
    p.add_argument("--source", choices=sources, default="places")
    p.add_argument("--limit", type=int, default=None, help="cap number of search tasks")

    p = sub.add_parser("enrich", help="crawl sites for email/certs/phone")
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("score", help="compute fit + priority tiers")
    p.add_argument("--vertical", choices=verticals, default="all")

    p = sub.add_parser("draft", help="draft introduction emails")
    p.add_argument("--tier", choices=["A", "B", "C"], default=None)
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("export", help="write the review CSV")
    p.add_argument("--out", default="data/leads.csv")
    p.add_argument("--tier", choices=["A", "B", "C"], default=None)
    p.add_argument(
        "--include-no-email",
        action="store_true",
        help="also export companies without a contact email",
    )

    p = sub.add_parser("run", help="full pipeline: discover -> export")
    p.add_argument("--vertical", choices=verticals, default="all")
    p.add_argument("--region", default="all")
    p.add_argument("--source", choices=sources, default="places")
    p.add_argument("--out", default="data/leads.csv")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--tier", choices=["A", "B", "C"], default=None)
    p.add_argument(
        "--include-no-email",
        action="store_true",
        help="also export companies without a contact email",
    )

    sub.add_parser("stats", help="summarise the database")

    args = parser.parse_args()
    settings = Settings()
    _apply_log_level(settings)
    conn = db.connect(settings.db_path)

    try:
        if args.command == "score":
            pipeline.run_score(conn, settings, vertical=args.vertical)
        elif args.command == "export":
            pipeline.run_export(
                conn, settings, out=Path(args.out), tier=args.tier,
                include_no_email=args.include_no_email,
            )
        elif args.command == "stats":
            _print_stats(db.stats(conn))
        else:
            sys.exit(asyncio.run(_run_async(args, settings, conn)))
    finally:
        conn.close()


def _print_stats(s: dict) -> None:
    print(f"Total companies:  {s['total']}")
    print(f"With email:       {s['with_email']}")
    print(f"Suppressed:       {s['suppressed']}")
    print(f"By enrichment:    {s['by_enrichment']}")
    print(f"By priority tier: {s['by_priority_tier']}")
    print(f"By draft status:  {s['by_draft_status']}")


if __name__ == "__main__":
    main()
