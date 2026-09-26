"""Validation command tests — operates against a hand-seeded apps table."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scanner import db as db_module
from scanner.cli import _run_validate
from scanner.config import Config


def _cfg(db_path: Path) -> Config:
    return Config(
        db_path=db_path,
        workers=1,
        log_level="WARNING",
        log_format="json",
        proxy_url="",
        sources={},
    )


async def _seed_apps(db_path: Path, rows: list[tuple[str, float]]) -> None:
    conn = await db_module.connect(db_path)
    try:
        for name, score in rows:
            await conn.execute(
                "INSERT INTO apps(name, platform, score) VALUES (?, 'unknown', ?)",
                (name, score),
            )
        await conn.commit()
    finally:
        await conn.close()


def _write_set(tmp_path: Path, hits_min: int, apps: list[dict]) -> Path:
    p = tmp_path / "validation_set.yaml"
    p.write_text(
        yaml.safe_dump({"min_hit_count": hits_min, "default_min_score": 1.5, "apps": apps}),
        encoding="utf-8",
    )
    return p


@pytest.mark.asyncio
async def test_validate_returns_zero_when_threshold_met(
    tmp_db: Path, migrations_dir: Path, tmp_path: Path
) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    await _seed_apps(
        tmp_db,
        [("Wunderlist", 3.0), ("Periscope", 2.5), ("Mailbox", 2.0)],
    )
    vs = _write_set(
        tmp_path,
        hits_min=2,
        apps=[{"name": "Wunderlist"}, {"name": "Periscope"}, {"name": "Picasa"}],
    )
    code = await _run_validate(_cfg(tmp_db), vs)
    assert code == 0  # 2 hits >= min_hit_count=2


@pytest.mark.asyncio
async def test_validate_returns_one_when_below_threshold(
    tmp_db: Path, migrations_dir: Path, tmp_path: Path
) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    await _seed_apps(tmp_db, [("Wunderlist", 0.5)])  # below default 1.5
    vs = _write_set(
        tmp_path,
        hits_min=2,
        apps=[{"name": "Wunderlist"}, {"name": "Periscope"}],
    )
    code = await _run_validate(_cfg(tmp_db), vs)
    assert code == 1


@pytest.mark.asyncio
async def test_validate_returns_two_when_file_missing(
    tmp_db: Path, migrations_dir: Path, tmp_path: Path
) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    code = await _run_validate(_cfg(tmp_db), tmp_path / "does-not-exist.yaml")
    assert code == 2


@pytest.mark.asyncio
async def test_validate_respects_per_entry_min_score(
    tmp_db: Path, migrations_dir: Path, tmp_path: Path
) -> None:
    await db_module.migrate(tmp_db, migrations_dir)
    await _seed_apps(tmp_db, [("Wunderlist", 1.2), ("Periscope", 1.2)])
    vs = _write_set(
        tmp_path,
        hits_min=1,
        apps=[
            {"name": "Wunderlist", "min_score": 1.0},  # 1.2 >= 1.0 -> HIT
            {"name": "Periscope", "min_score": 1.5},  # 1.2 <  1.5 -> MISS
        ],
    )
    code = await _run_validate(_cfg(tmp_db), vs)
    assert code == 0  # 1 hit >= min_hit_count=1


@pytest.mark.asyncio
async def test_real_validation_set_is_well_formed() -> None:
    """Sanity-check the repo-tracked validation set itself: parses, has
    >= min_hit_count entries, and each entry has a valid min_score (if set)."""
    path = Path(__file__).resolve().parent / "validation_set.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "apps" in data and isinstance(data["apps"], list)
    assert data["min_hit_count"] <= len(data["apps"])
    for entry in data["apps"]:
        assert "name" in entry and entry["name"]
        if "min_score" in entry:
            assert isinstance(entry["min_score"], int | float)
            assert entry["min_score"] > 0
