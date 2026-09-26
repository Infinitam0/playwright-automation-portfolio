"""Score apps from accumulated signals.

The score answers two questions in one number:
- *Popular?* — mention volume + cross-source breadth.
- *Discontinued?* — shutdown-vocab weight + AlternativeTo's discontinued badge.

Each is decayed by recency so a 2017 shutdown does not keep ranking forever.

Formula per the plan (`scanner/score/app_scorer.py` in the plan file):

    score = (log1p(m) * (1 + 0.5*(s - 1)) + 2.0*shut + 0.5*alt + 1.5*badge) * decay

where
    m      = distinct inbox rows mentioning this app within `window_days`
    s      = distinct sources mentioning this app within `window_days`
    shut   = max signal confidence in {shutdown, dead, disc, eol, rip}
    alt    = max signal confidence in {alt_to, replace, instead, moving}
    badge  = 1.0 if AlternativeTo marks the app discontinued, else 0.0
    decay  = exp(-age_days_of_latest_signal / 365)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from scanner.extract.catalog import Catalog
from scanner.logging import get_logger

log = get_logger(__name__)


SHUTDOWN_TYPES = ("shutdown", "dead", "disc", "eol", "rip")
ALT_TYPES = ("alt_to", "replace", "instead", "moving")


@dataclass
class AppStats:
    mentions: int = 0
    sources: int = 0
    shut: float = 0.0
    alt: float = 0.0
    latest_iso: str | None = None
    badge: bool = False


def compute_score(stats: AppStats, now: datetime | None = None) -> float:
    """Pure scoring function. Easy to test in isolation."""
    if stats.mentions <= 0:
        return 0.0
    now = now or datetime.now(UTC)
    age_days = 0.0
    if stats.latest_iso:
        try:
            latest = datetime.fromisoformat(stats.latest_iso.replace("Z", "+00:00"))
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            age_days = max(0.0, (now - latest).total_seconds() / 86400.0)
        except ValueError:
            pass
    decay = math.exp(-age_days / 365.0)
    volume_term = math.log1p(stats.mentions) * (1.0 + 0.5 * max(0, stats.sources - 1))
    badge_term = 1.5 if stats.badge else 0.0
    return (volume_term + 2.0 * stats.shut + 0.5 * stats.alt + badge_term) * decay


class AppScorer:
    def __init__(self, catalog: Catalog | None = None, window_days: int = 180) -> None:
        self._catalog = catalog or Catalog.load()
        self._window_days = window_days

    async def update(self, conn: aiosqlite.Connection, app: str) -> float:
        stats = await self._stats(conn, app)
        stats.badge = self._catalog.is_discontinued(app)
        score = compute_score(stats)
        await self._upsert(conn, app, stats, score)
        return score

    async def update_all_mentioned(self, conn: aiosqlite.Connection) -> int:
        cur = await conn.execute("SELECT DISTINCT mentioned_app FROM signals")
        apps = [r[0] for r in await cur.fetchall()]
        await cur.close()
        for app in apps:
            await self.update(conn, app)
        log.info("score.updated", count=len(apps))
        return len(apps)

    async def _stats(self, conn: aiosqlite.Connection, app: str) -> AppStats:
        cur = await conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT s.inbox_id),
                COUNT(DISTINCT i.source),
                MAX(CASE WHEN s.signal_type IN {SHUTDOWN_TYPES!r}
                         THEN s.confidence END),
                MAX(CASE WHEN s.signal_type IN {ALT_TYPES!r}
                         THEN s.confidence END),
                MAX(i.scraped_at)
            FROM signals s
            JOIN inbox i ON i.id = s.inbox_id
            WHERE s.mentioned_app = ?
              AND i.scraped_at >= datetime('now', '-{int(self._window_days)} days')
            """,
            (app,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return AppStats()
        return AppStats(
            mentions=row[0] or 0,
            sources=row[1] or 0,
            shut=float(row[2] or 0.0),
            alt=float(row[3] or 0.0),
            latest_iso=row[4],
        )

    async def _upsert(
        self,
        conn: aiosqlite.Connection,
        app: str,
        stats: AppStats,
        score: float,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO apps(name, platform, score, mention_count, source_count,
                             alttto_discontinued, last_seen)
            VALUES (?, 'unknown', ?, ?, ?, ?, ?)
            ON CONFLICT(name, platform) DO UPDATE SET
                score=excluded.score,
                mention_count=excluded.mention_count,
                source_count=excluded.source_count,
                alttto_discontinued=excluded.alttto_discontinued,
                last_seen=excluded.last_seen
            """,
            (
                app,
                score,
                stats.mentions,
                stats.sources,
                int(stats.badge),
                stats.latest_iso,
            ),
        )
        await conn.commit()
