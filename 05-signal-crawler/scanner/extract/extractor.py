"""Inbox → signals extraction pipeline.

Pure function over the inbox table: same inbox row produces the same signal
rows every time. This makes `scanner rebuild` the safety net — drop signals
+ apps, re-run, get a deterministic rebuild.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from scanner.extract.catalog import Catalog, disambiguate
from scanner.extract.patterns import find_all
from scanner.logging import get_logger

if TYPE_CHECKING:
    from scanner.base import BaseScraper

log = get_logger(__name__)


class Extractor:
    """Stateful for catalog reuse; one instance per CLI invocation is fine."""

    def __init__(
        self,
        scrapers: dict[str, type[BaseScraper]],
        catalog: Catalog | None = None,
    ) -> None:
        self._scrapers = scrapers
        self._catalog = catalog or Catalog.load()

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    async def process_one(self, conn: aiosqlite.Connection, inbox_id: int) -> int:
        """Extract signals for one inbox row. Returns the number of signals inserted."""
        cur = await conn.execute("SELECT source, raw_content FROM inbox WHERE id = ?", (inbox_id,))
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return 0
        source, raw_content = row[0], row[1]

        scraper_cls = self._scrapers.get(source)
        if scraper_cls is None:
            log.warning("extract.unknown_source", source=source, inbox_id=inbox_id)
            text = raw_content
        else:
            text = scraper_cls.text(raw_content)

        signals_inserted = 0
        for signal_type, signal_text, candidate, conf in find_all(text):
            canonical = disambiguate(candidate, conf, self._catalog)
            if canonical is None:
                continue
            cur = await conn.execute(
                """
                INSERT INTO signals(inbox_id, signal_type, signal_text, mentioned_app, confidence)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(inbox_id, signal_type, signal_text) DO NOTHING
                """,
                (inbox_id, signal_type, signal_text[:500], canonical, conf),
            )
            if cur.rowcount > 0:
                signals_inserted += 1
            await cur.close()

        await conn.execute("UPDATE inbox SET extracted = 1 WHERE id = ?", (inbox_id,))
        await conn.commit()
        return signals_inserted

    async def process_pending(
        self, conn: aiosqlite.Connection, limit: int | None = None
    ) -> tuple[int, int]:
        """Extract every inbox row with extracted=0. Returns (rows_processed, signals_inserted)."""
        q = "SELECT id FROM inbox WHERE extracted = 0 ORDER BY id"
        if limit is not None:
            q += f" LIMIT {int(limit)}"
        cur = await conn.execute(q)
        ids = [r[0] for r in await cur.fetchall()]
        await cur.close()

        rows_done = 0
        sigs_total = 0
        for inbox_id in ids:
            sigs = await self.process_one(conn, inbox_id)
            sigs_total += sigs
            rows_done += 1
        log.info("extract.batch_done", rows=rows_done, signals=sigs_total)
        return rows_done, sigs_total

    async def reset_and_reprocess(self, conn: aiosqlite.Connection) -> tuple[int, int]:
        """Truncate signals + apps, mark every inbox row as unextracted, then
        reprocess. The user-visible safety net (`scanner rebuild`).
        Returns (rows_processed, signals_inserted).
        """
        await conn.execute("DELETE FROM signals")
        await conn.execute("DELETE FROM apps")
        await conn.execute("UPDATE inbox SET extracted = 0")
        await conn.commit()
        return await self.process_pending(conn)


async def mentioned_apps_since(conn: aiosqlite.Connection, since_inbox_id: int = 0) -> set[str]:
    """Distinct app names in signals attached to inbox rows id > since_inbox_id.
    Used by the scorer to know which apps to re-score."""
    cur = await conn.execute(
        "SELECT DISTINCT mentioned_app FROM signals s "
        "JOIN inbox i ON i.id = s.inbox_id "
        "WHERE i.id > ?",
        (since_inbox_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return {r[0] for r in rows}
