"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Per-test SQLite path under tmp_path. File is created on first connection."""
    return tmp_path / "scanner.sqlite"


@pytest.fixture
def migrations_dir() -> Path:
    """Repository migrations directory (real SQL files)."""
    return Path(__file__).resolve().parent.parent / "migrations"
