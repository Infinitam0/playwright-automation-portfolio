"""Shared data classes that cross module boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawItem:
    """A single post yielded by a scraper. Becomes one row in the inbox table.

    `raw_content` is the verbatim source text used for both extraction and
    deduplication (via the SHA-256 hash). When `cursor` is set, the orchestrator
    commits it to the cursors table after the inbox upsert succeeds — this is
    how scrapers express durable progress.
    """

    source: str
    source_item_id: str
    raw_content: str
    url: str | None = None
    cursor: str | None = None

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.raw_content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Job:
    """A claimed unit of work for one worker. Mirrors a row in the jobs table."""

    id: int
    source: str
    cursor: str | None
    status: str
    claimed_by: str | None
    claimed_at: datetime | None
    attempts: int
    error_msg: str | None
    run_after: datetime | None


@dataclass(frozen=True, slots=True)
class Signal:
    """One extracted replacement-seeking mention. Mirrors a row in signals."""

    inbox_id: int
    signal_type: str
    signal_text: str
    mentioned_app: str
    confidence: float


@dataclass(frozen=True, slots=True)
class AppRow:
    """A ranked discontinued-candidate. Mirrors a row in apps."""

    name: str
    platform: str
    score: float
    mention_count: int
    source_count: int
    alttto_discontinued: bool
    last_seen: datetime | None = None
