"""Configuration loader. tomllib (stdlib) → frozen dataclass. No env-var fallbacks."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    enabled: bool
    rate_per_min: int


@dataclass(frozen=True, slots=True)
class Config:
    db_path: Path
    workers: int
    log_level: str
    log_format: str
    proxy_url: str
    sources: dict[str, SourceConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> Config:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))

        db = raw.get("db", {})
        runtime = raw.get("runtime", {})
        proxy = raw.get("proxy", {})
        sources_raw = raw.get("sources", {})

        sources = {
            name: SourceConfig(
                name=name,
                enabled=bool(s.get("enabled", True)),
                rate_per_min=int(s.get("rate_per_min", 30)),
            )
            for name, s in sources_raw.items()
        }

        return cls(
            db_path=Path(db.get("path", "./scanner.sqlite")),
            workers=int(runtime.get("workers", 3)),
            log_level=str(runtime.get("log_level", "INFO")).upper(),
            log_format=str(runtime.get("log_format", "json")),
            proxy_url=str(proxy.get("url", "")),
            sources=sources,
        )
