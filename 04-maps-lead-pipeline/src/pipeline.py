"""Orchestration: discover -> enrich -> score -> draft -> export.

Each stage is independently runnable (the CLI exposes them) and `run_all`
chains them. Discovery is sequential (polite; the Places source is one host);
enrichment fans out under the shared per-host rate limiter with a concurrency
cap.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from . import db
from .config import Settings
from .discovery.base import DiscoverySource, SearchTask
from .discovery.google_places import GooglePlacesSource
from .enrich.website import enrich_company
from .loaders import (
    load_email_prompt,
    load_exclusions,
    load_regions,
    load_suppression,
    load_verticals,
)
from .models import VERTICALS, Company
from .net.http import HttpClient
from .outreach.draft import build_fields, get_drafter
from .outreach.export import export_csv
from .outreach.lint import lint_draft
from .score import score_company

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers ----
def _verticals_for(vertical: str) -> list[str]:
    if vertical == "all":
        return [*VERTICALS, "generic"]
    return [vertical]


def _needed_verticals(vertical: str) -> list[str]:
    return list(VERTICALS) if vertical == "all" else [vertical]


def _slug(s: str) -> str:
    return s.lower().replace(" ", "-")


def resolve_cities(regions_cfg: dict, region: str) -> list[tuple[str, str, int]]:
    """Return [(city, province_slug, tier)] for a region arg: all | <province> | <city>."""
    provinces = regions_cfg.get("provinces", {})
    out: list[tuple[str, str, int]] = []
    region_l = region.lower()
    if region_l in ("all", ""):
        for prov, cfg in provinces.items():
            for city in cfg.get("cities", []):
                out.append((city, prov, cfg.get("priority_tier", 3)))
        return out
    if region_l in provinces:
        cfg = provinces[region_l]
        return [(c, region_l, cfg.get("priority_tier", 3)) for c in cfg.get("cities", [])]
    # treat as a single city; find its province/tier if we know it
    for prov, cfg in provinces.items():
        for city in cfg.get("cities", []):
            if city.lower() == region_l:
                return [(city, prov, cfg.get("priority_tier", 3))]
    return [(region, "", 3)]  # unknown city, still usable


def province_tier(regions_cfg: dict, province: str) -> int:
    key = _slug(province)
    cfg = regions_cfg.get("provinces", {}).get(key)
    if cfg:
        return cfg.get("priority_tier", 3)
    return 3


def build_search_tasks(
    verticals_cfg: dict, regions_cfg: dict, *, vertical: str, region: str
) -> list[SearchTask]:
    cities = resolve_cities(regions_cfg, region)
    tasks: list[SearchTask] = []
    for v in _verticals_for(vertical):
        terms = verticals_cfg.get(v, {}).get("search_terms", [])
        if not terms:
            continue
        primary = terms[0]  # one representative query per vertical/city (cost bound)
        for city, prov, _tier in cities:
            tasks.append(
                SearchTask(query=f"{primary} {city}", vertical=v, city=city, province=prov)
            )
    return tasks


def build_sources(
    names: list[str], http: HttpClient, settings: Settings
) -> list[DiscoverySource]:
    sources: list[DiscoverySource] = []
    want = set(names)
    if "places" in want:
        if settings.google_maps_api_key:
            sources.append(
                GooglePlacesSource(
                    http,
                    settings.google_maps_api_key,
                    max_pages=settings.places_max_pages,
                    language=settings.places_language,
                    region=settings.places_region,
                )
            )
        else:
            logger.critical(
                "places requested but GOOGLE_MAPS_API_KEY is unset — skipping. "
                "Set GOOGLE_MAPS_API_KEY to enable the Places source."
            )
    if "maps" in want:
        from .discovery.google_maps_html import GoogleMapsHtmlSource

        sources.append(
            GoogleMapsHtmlSource(
                Path("auth/maps_profile"), enabled=settings.maps_enabled
            )
        )
    return sources


async def _bounded_gather(factories, n: int):
    sem = asyncio.Semaphore(n)

    async def wrap(factory):
        async with sem:
            return await factory()

    return await asyncio.gather(*(wrap(f) for f in factories), return_exceptions=True)


# ----------------------------------------------------------------- stages ----
async def run_discover(
    conn: sqlite3.Connection,
    http: HttpClient,
    settings: Settings,
    *,
    vertical: str,
    region: str,
    source: str,
    limit: int | None = None,
) -> int:
    verticals_cfg = load_verticals(settings.config_dir)
    regions_cfg = load_regions(settings.config_dir)
    tasks = build_search_tasks(
        verticals_cfg, regions_cfg, vertical=vertical, region=region
    )
    if limit:
        tasks = tasks[:limit]
    sources = build_sources([source], http, settings)
    if not sources:
        logger.error("No discovery sources available — check credentials/config.")
        return 0

    logger.info(f"discover: {len(tasks)} tasks x {len(sources)} sources")
    found = 0
    for task in tasks:
        for src in sources:
            try:
                candidates = await src.search(task)
            except Exception as e:  # noqa: BLE001
                logger.info(f"discover: {src.name} failed on '{task.query}': {e}")
                continue
            for cand in candidates:
                company = cand.to_company(vertical_hint=task.vertical)
                if not company.province:
                    company.province = task.province
                db.upsert_company(conn, company)
                found += 1
    logger.info(f"discover: {found} candidate hits upserted")
    return found


async def run_enrich(
    conn: sqlite3.Connection,
    http: HttpClient,
    settings: Settings,
    *,
    limit: int | None = None,
) -> int:
    verticals_cfg = load_verticals(settings.config_dir)
    exclusions = load_exclusions(settings.config_dir)
    pending = db.iter_companies(conn, enrichment_status="pending", limit=limit)
    if not pending:
        logger.info("enrich: nothing pending")
        return 0

    async def _one(company: Company):
        await enrich_company(
            company,
            fetch=http.get_text,
            verticals_cfg=verticals_cfg,
            settings=settings,
            negative_keywords=exclusions["out_of_scope_keywords"],
            segment_signals=exclusions["segment_signals"],
        )
        db.save_company(conn, company)

    logger.info(f"enrich: {len(pending)} companies")
    results = await _bounded_gather(
        [lambda c=c: _one(c) for c in pending], settings.max_concurrency
    )
    for company, result in zip(pending, results):
        if isinstance(result, Exception):
            logger.warning(f"enrich: {company.name} failed: {result!r}")
    return len(pending)


def run_score(conn: sqlite3.Connection, settings: Settings, *, vertical: str) -> int:
    regions_cfg = load_regions(settings.config_dir)
    exclusions = load_exclusions(settings.config_dir)
    suppression = load_suppression(settings.config_dir)
    needed = _needed_verticals(vertical)
    companies = db.iter_companies(conn)
    for c in companies:
        tier = province_tier(regions_cfg, c.province)
        score_company(
            c,
            exclusions=exclusions,
            suppression=suppression,
            needed_verticals=needed,
            province_tier=tier,
            settings=settings,
        )
        db.save_company(conn, c)
    logger.info(f"score: scored {len(companies)} companies")
    return len(companies)


async def run_draft(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
) -> int:
    """Draft every undrafted, emailable, unsuppressed company.

    Subject variants A/B alternate in draft order within the run (an A/B test
    per batch).
    """
    verticals_cfg = load_verticals(settings.config_dir)
    system, user = load_email_prompt(settings.config_dir)
    drafter = get_drafter(settings, system, user)
    logger.info(f"draft: using {getattr(drafter, 'name', '?')} drafter")

    # Throttle only a network-bound drafter (the API-calling AnthropicDrafter);
    # the offline template drafter runs at full speed. Interval is measured from
    # the start of one API call to the start of the next, so a slow call eats
    # into the wait rather than adding to it.
    interval = (
        settings.draft_min_interval_seconds
        if getattr(drafter, "network_bound", False)
        else 0.0
    )
    next_call_at = 0.0  # monotonic deadline; 0 => first call is immediate

    targets = db.iter_companies(
        conn, min_tier=tier, exclude_suppressed=True, limit=limit
    )
    drafted = attempted = 0
    for c in targets:
        if c.draft_status == "drafted":
            continue
        if not c.best_email():
            continue  # no address -> cannot cold-email
        if interval > 0:
            wait = next_call_at - time.monotonic()
            if wait > 0:
                logger.info(
                    f"draft: throttling {wait:.0f}s before next draft "
                    f"(1 per {interval:.0f}s)"
                )
                await asyncio.sleep(wait)
            next_call_at = time.monotonic() + interval
        fields = build_fields(
            c, verticals_cfg, settings, subject_variant="AB"[attempted % 2]
        )
        attempted += 1
        result = await drafter.draft_one(c, fields)
        if result is None:
            c.draft_status = "failed"
        else:
            subject, body = result
            c.draft_subject, c.draft_body = subject, body
            c.draft_model = getattr(drafter, "name", "")
            violations = lint_draft(subject, body)
            # The footer carries sender identity, reason-for-receipt and
            # opt-out. A model that re-wraps or trims it can keep the word
            # "unsubscribe" and still lose the rest.
            if fields["sender_block"] not in body:
                violations.append("sign-off block was altered or dropped")
            if violations:
                logger.warning(f"draft: lint issues for {c.name}: {violations}")
                c.draft_status = "needs_review"
            else:
                c.draft_status = "drafted"
            drafted += 1
        db.save_company(conn, c)
    logger.info(f"draft: {drafted} emails drafted")
    return drafted


# A draft that failed the lint (or errored) must never reach the
# CSV: the operator sends from that file, so an unsendable draft sitting in it is
# one copy-paste away from going out. `none` still exports — exporting before the
# draft stage is a legitimate flow, and such a row carries no draft text at all.
UNSENDABLE_DRAFT_STATUSES = frozenset({"needs_review", "failed"})


def run_export(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    out: Path,
    tier: str | None = None,
    include_no_email: bool = False,
) -> int:
    companies = db.iter_companies(conn, min_tier=tier, exclude_suppressed=True)
    if not include_no_email:
        companies = [c for c in companies if c.best_email()]
    withheld = [c for c in companies if c.draft_status in UNSENDABLE_DRAFT_STATUSES]
    if withheld:
        companies = [
            c for c in companies if c.draft_status not in UNSENDABLE_DRAFT_STATUSES
        ]
        logger.warning(
            f"export: withheld {len(withheld)} companies whose draft did not pass "
            f"lint or failed to generate. Re-run `draft` after fixing the cause "
            f"(unset sender identity is the usual one). Withheld: "
            + ", ".join(f"{c.name} [{c.draft_status}]" for c in withheld[:5])
            + (" ..." if len(withheld) > 5 else "")
        )
    n = export_csv(companies, out)
    logger.info(f"export: {n} companies -> {out}")
    return n


async def run_all(
    conn: sqlite3.Connection,
    http: HttpClient,
    settings: Settings,
    *,
    vertical: str,
    region: str,
    source: str,
    out: Path,
    limit: int | None = None,
    tier: str | None = None,
    include_no_email: bool = False,
) -> int:
    found = await run_discover(
        conn, http, settings, vertical=vertical, region=region, source=source, limit=limit
    )
    if not found:
        # Nothing discovered (e.g. missing API key): don't write a header-only CSV.
        # main() turns the 0 return into a loud non-zero exit.
        return 0
    await run_enrich(conn, http, settings)
    run_score(conn, settings, vertical=vertical)
    await run_draft(conn, settings, tier=tier)
    return run_export(
        conn, settings, out=out, tier=tier, include_no_email=include_no_email
    )
