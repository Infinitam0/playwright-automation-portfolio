"""Typer CLI surface. Each command resolves config, configures logging, then
dispatches to async machinery via asyncio.run()."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from scanner import db as db_module
from scanner.config import Config
from scanner.extract.catalog import Catalog
from scanner.extract.extractor import Extractor
from scanner.logging import configure as configure_logging
from scanner.logging import get_logger
from scanner.orchestrator import Orchestrator
from scanner.registry import load_all
from scanner.score.app_scorer import AppScorer

app = typer.Typer(
    help="Signal Crawler — find mobile apps people are asking to replace.",
    no_args_is_help=True,
    add_completion=False,
)

sources_app = typer.Typer(help="Manage scraper sources.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")


def _load(config_path: Path) -> Config:
    cfg = Config.load(config_path)
    configure_logging(level=cfg.log_level, fmt=cfg.log_format)
    return cfg


# ---------- migrate ----------


@app.command()
def migrate(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    migrations_dir: Path = typer.Option(Path("migrations"), "--migrations-dir"),
) -> None:
    """Apply pending SQL migrations to the database."""
    cfg = _load(config)
    log = get_logger(__name__)
    log.info("migrate.start", db_path=str(cfg.db_path), migrations_dir=str(migrations_dir))
    applied = asyncio.run(db_module.migrate(cfg.db_path, migrations_dir))
    if applied:
        typer.echo(f"Applied {len(applied)} migration(s): {applied}")
    else:
        typer.echo("Database is up to date.")


# ---------- run ----------


@app.command()
def run(
    once: bool = typer.Option(False, "--once", help="Drain pending jobs and exit."),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Start the scraper orchestrator."""
    cfg = _load(config)
    scrapers = load_all()
    if not scrapers:
        typer.echo("No scrapers registered under scanner/scrapers/. Nothing to do.", err=True)
        raise typer.Exit(code=2)
    orch = Orchestrator(cfg, scrapers)
    asyncio.run(orch.run(once=once))


# ---------- status ----------


@app.command()
def status(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Print active jobs, last cursor per source, inbox-pending counts."""
    cfg = _load(config)
    asyncio.run(_print_status(cfg))


async def _print_status(cfg: Config) -> None:
    conn = await db_module.connect(cfg.db_path)
    try:
        rows = await (
            await conn.execute(
                "SELECT source, status, COUNT(*) c FROM jobs "
                "GROUP BY source, status ORDER BY source, status"
            )
        ).fetchall()
        typer.echo("Jobs (source / status / count):")
        if not rows:
            typer.echo("  (none)")
        for r in rows:
            typer.echo(f"  {r[0]:<16} {r[1]:<8} {r[2]}")

        typer.echo("\nCursors:")
        rows = await (
            await conn.execute("SELECT source, max_id, processed_at FROM cursors ORDER BY source")
        ).fetchall()
        if not rows:
            typer.echo("  (none)")
        for r in rows:
            typer.echo(f"  {r[0]:<16} cursor={r[1]!r:<24} processed_at={r[2]}")

        typer.echo("\nInbox:")
        row = await (
            await conn.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN extracted=0 THEN 1 ELSE 0 END) pending FROM inbox"
            )
        ).fetchone()
        typer.echo(f"  total={row[0] or 0}  pending_extract={row[1] or 0}")

        typer.echo("\nControl:")
        rows = await (await conn.execute("SELECT key, value FROM control ORDER BY key")).fetchall()
        if not rows:
            typer.echo("  (none)")
        for r in rows:
            typer.echo(f"  {r[0]} = {r[1]}")
    finally:
        await conn.close()


# ---------- pause / resume ----------


@app.command()
def pause(
    source: str | None = typer.Option(
        None, "--source", "-s", help="Specific source to pause; omit for global pause."
    ),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Set a control flag so workers stop claiming jobs."""
    cfg = _load(config)
    key = f"paused:{source}" if source else "paused"
    asyncio.run(_set_control(cfg, key, "1"))
    typer.echo(f"Paused: {key}")


@app.command()
def resume(
    source: str | None = typer.Option(
        None, "--source", "-s", help="Specific source to resume; omit for global."
    ),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Clear the pause control flag so workers resume claiming jobs."""
    cfg = _load(config)
    key = f"paused:{source}" if source else "paused"
    asyncio.run(_set_control(cfg, key, "0"))
    typer.echo(f"Resumed: {key}")


async def _set_control(cfg: Config, key: str, value: str) -> None:
    conn = await db_module.connect(cfg.db_path)
    try:
        await conn.execute(
            "INSERT INTO control(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()
    finally:
        await conn.close()


# ---------- sources ----------


@sources_app.command("list")
def sources_list(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """List all sources registered in the database."""
    cfg = _load(config)
    asyncio.run(_print_sources(cfg))


async def _print_sources(cfg: Config) -> None:
    scrapers = load_all()
    conn = await db_module.connect(cfg.db_path)
    try:
        rows = await (
            await conn.execute(
                "SELECT name, enabled, status, rate_per_min FROM sources ORDER BY name"
            )
        ).fetchall()
        typer.echo("Database-registered sources:")
        if not rows:
            typer.echo("  (none -- run `scanner run --once` to seed)")
        for r in rows:
            in_registry = "[loaded]" if r[0] in scrapers else "[no-scraper]"
            typer.echo(
                f"  {r[0]:<16} enabled={r[1]} status={r[2]:<14} rate={r[3]:<3}/min  {in_registry}"
            )

        loaded_only = sorted(set(scrapers) - {r[0] for r in rows})
        if loaded_only:
            typer.echo("\nScrapers loaded but not yet in DB:")
            for n in loaded_only:
                typer.echo(f"  {n}")
    finally:
        await conn.close()


# ---------- extract / rebuild / apps ----------

apps_app = typer.Typer(help="Inspect ranked discontinued candidates.", no_args_is_help=True)
app.add_typer(apps_app, name="apps")


@app.command()
def extract(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
    limit: int | None = typer.Option(None, "--limit", help="Cap on rows processed."),
) -> None:
    """Extract signals from pending inbox rows; rescore mentioned apps."""
    cfg = _load(config)
    asyncio.run(_run_extract(cfg, limit, reset=False))


@app.command()
def rebuild(
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Truncate signals + apps and re-derive them from inbox. The safety net."""
    cfg = _load(config)
    asyncio.run(_run_extract(cfg, limit=None, reset=True))


async def _run_extract(cfg: Config, limit: int | None, *, reset: bool) -> None:
    scrapers = load_all()
    catalog = Catalog.load()
    extractor = Extractor(scrapers=scrapers, catalog=catalog)
    scorer = AppScorer(catalog=catalog)

    conn = await db_module.connect(cfg.db_path)
    try:
        if reset:
            rows, sigs = await extractor.reset_and_reprocess(conn)
        else:
            rows, sigs = await extractor.process_pending(conn, limit=limit)
        scored = await scorer.update_all_mentioned(conn)
        typer.echo(
            f"Processed {rows} inbox row(s); inserted {sigs} signal(s); scored {scored} app(s)."
        )
    finally:
        await conn.close()


@app.command()
def validate(
    validation_set: Path = typer.Option(
        Path("tests/validation_set.yaml"),
        "--set",
        help="Path to validation_set.yaml.",
    ),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Check the apps table against the validation set; exit non-zero on insufficient hits."""
    cfg = _load(config)
    exit_code = asyncio.run(_run_validate(cfg, validation_set))
    raise typer.Exit(code=exit_code)


async def _run_validate(cfg: Config, validation_set_path: Path) -> int:
    import yaml

    if not validation_set_path.exists():
        typer.echo(f"validation set not found: {validation_set_path}", err=True)
        return 2
    data = yaml.safe_load(validation_set_path.read_text(encoding="utf-8")) or {}
    min_hit_count = int(data.get("min_hit_count", 6))
    default_min = float(data.get("default_min_score", 1.5))
    entries = data.get("apps", []) or []
    if not entries:
        typer.echo("validation set is empty", err=True)
        return 2

    conn = await db_module.connect(cfg.db_path)
    try:
        results: list[tuple[str, float | None, float, str]] = []
        hits = 0
        for entry in entries:
            name = entry["name"]
            threshold = float(entry.get("min_score", default_min))
            cur = await conn.execute(
                "SELECT score FROM apps WHERE name=? ORDER BY score DESC LIMIT 1",
                (name,),
            )
            row = await cur.fetchone()
            await cur.close()
            score = float(row[0]) if row is not None else None
            verdict = "HIT" if (score is not None and score >= threshold) else "MISS"
            if verdict == "HIT":
                hits += 1
            results.append((name, score, threshold, verdict))
    finally:
        await conn.close()

    typer.echo(f"{'app':<26} {'score':>7} {'min':>5}  verdict")
    for name, score, threshold, verdict in results:
        score_str = f"{score:>7.2f}" if score is not None else f"{'  -  ':>7}"
        typer.echo(f"{name:<26} {score_str} {threshold:>5.2f}  {verdict}")
    typer.echo(f"\n{hits}/{len(results)} hit (min required: {min_hit_count})")

    return 0 if hits >= min_hit_count else 1


@apps_app.command("top")
def apps_top(
    n: int = typer.Option(20, "--n", "-n", help="How many to show."),
    min_score: float = typer.Option(0.0, "--min-score", "-m"),
    config: Path = typer.Option(Path("config.toml"), "--config", "-c"),
) -> None:
    """Print ranked discontinued-app candidates."""
    cfg = _load(config)
    asyncio.run(_print_apps_top(cfg, n=n, min_score=min_score))


async def _print_apps_top(cfg: Config, *, n: int, min_score: float) -> None:
    conn = await db_module.connect(cfg.db_path)
    try:
        rows = await (
            await conn.execute(
                """
            SELECT name, platform, score, mention_count, source_count,
                   alttto_discontinued, last_seen
            FROM apps
            WHERE score >= ?
            ORDER BY score DESC
            LIMIT ?
            """,
                (min_score, n),
            )
        ).fetchall()
        if not rows:
            typer.echo("No apps scored yet. Run `scanner extract` after `scanner run`.")
            return
        typer.echo(
            f"{'name':<30} {'plat':<8} {'score':>6} {'m':>4} {'src':>3} {'alt':>3} last_seen"
        )
        for r in rows:
            badge = "Y" if r[5] else "."
            typer.echo(
                f"{r[0][:30]:<30} {r[1][:8]:<8} {r[2]:>6.2f} "
                f"{r[3]:>4} {r[4]:>3} {badge:>3} {r[6] or ''}"
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    app()
