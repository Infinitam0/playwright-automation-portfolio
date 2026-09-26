"""SQLite persistence, so an interrupted run resumes instead of restarting.

A city-wide grid is thousands of deliberately-paced detail loads — hours of
wall clock. It will be interrupted: a challenge page, a laptop sleeping, a typo
in a later stage. Without this, every interruption throws away the expensive part
and re-hits Google for data already collected, which is both slow and the fastest
way to earn a block.

Keyed on the feature id, which the feed gives us for free before any detail load.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .models import Place, PlaceDetail, WebsiteTier

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    fid          TEXT PRIMARY KEY,
    cid          TEXT,
    name         TEXT,
    maps_url     TEXT NOT NULL,
    tile         TEXT,
    detail_state TEXT NOT NULL DEFAULT 'pending',
    first_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS details (
    fid           TEXT PRIMARY KEY REFERENCES places(fid),
    name          TEXT,
    cid           TEXT,
    place_id      TEXT,
    website       TEXT,
    website_tier  TEXT,
    phone         TEXT,
    address       TEXT,
    category      TEXT,
    rating        REAL,
    review_count  INTEGER,
    plus_code     TEXT,
    latitude      REAL,
    longitude     REAL,
    maps_url      TEXT,
    misses        TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Run-level settings that must stay identical across resumes.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_places_state ON places(detail_state);
CREATE INDEX IF NOT EXISTS idx_details_tier ON details(website_tier);

-- Harvested tiles, so a resumed run does not re-scroll them.
-- Keyed on (query, label): the same map position must still be searched once per
-- query. Keying on position alone would make a multi-category sweep silently
-- skip every tile after the first category and return almost nothing.
CREATE TABLE IF NOT EXISTS tiles (
    query      TEXT NOT NULL,
    label      TEXT NOT NULL,
    found      INTEGER NOT NULL,
    saturated  INTEGER NOT NULL DEFAULT 0,
    done_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (query, label)
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def remembered_centre(self, location: str) -> tuple[float, float] | None:
        """The centre resolved the first time this location was swept.

        Nominatim is not deterministic — the same query has returned two centres
        roughly 2 km apart on different days. Since tiles are labelled by coordinate, a
        drifting centre relabels every tile and makes a resumed run re-harvest
        the whole grid. Pin it on first run and reuse it forever after.
        """
        raw = self.get_meta(f"centre:{location.strip().lower()}")
        if not raw:
            return None
        lat, lng = raw.split(",")
        return float(lat), float(lng)

    def remember_centre(self, location: str, lat: float, lng: float) -> None:
        self.set_meta(f"centre:{location.strip().lower()}", f"{lat},{lng}")

    # -- phase 1 ----------------------------------------------------------

    def add_places(self, places: list[Place]) -> int:
        """Insert harvested places, ignoring ones already known.

        Returns how many were new. Grid tiles overlap heavily by design, so this
        is where most duplicate work gets eliminated — before it costs anything.
        """
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO places (fid, cid, name, maps_url, tile) "
            "VALUES (?, ?, ?, ?, ?)",
            [(p.fid, p.cid, p.name, p.maps_url, p.tile) for p in places],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def record_tile(self, label: str, query: str, found: int, saturated: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tiles (query, label, found, saturated) VALUES (?,?,?,?)",
            (query, label, found, int(saturated)),
        )
        self.conn.commit()

    def tile_done(self, label: str, query: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM tiles WHERE query = ? AND label = ?", (query, label)
            ).fetchone()
            is not None
        )

    # -- phase 2 ----------------------------------------------------------

    def pending_places(self, limit: int | None = None) -> list[Place]:
        sql = "SELECT * FROM places WHERE detail_state = 'pending' ORDER BY first_seen"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            Place(
                fid=r["fid"], maps_url=r["maps_url"], name=r["name"],
                cid=r["cid"], tile=r["tile"],
            )
            for r in self.conn.execute(sql)
        ]

    def save_detail(self, detail: PlaceDetail) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO details
               (fid, name, cid, place_id, website, website_tier, phone, address,
                category, rating, review_count, plus_code, latitude, longitude,
                maps_url, misses)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                detail.fid, detail.name, detail.cid, detail.place_id, detail.website,
                str(detail.website_tier), detail.phone, detail.address, detail.category,
                detail.rating, detail.review_count, detail.plus_code, detail.latitude,
                detail.longitude, detail.maps_url, ", ".join(detail.extraction_misses),
            ),
        )
        self.conn.execute(
            "UPDATE places SET detail_state = 'done' WHERE fid = ?", (detail.fid,)
        )
        self.conn.commit()

    def mark_failed(self, fid: str) -> None:
        """Mark a place we could not read.

        Kept distinct from 'done' with a null website: a fetch failure says
        nothing about whether the business has a site, and must never reach the
        lead list as though it did.
        """
        self.conn.execute(
            "UPDATE places SET detail_state = 'failed' WHERE fid = ?", (fid,)
        )
        self.conn.commit()

    def reset_failed(self) -> int:
        cur = self.conn.execute(
            "UPDATE places SET detail_state = 'pending' WHERE detail_state = 'failed'"
        )
        self.conn.commit()
        return cur.rowcount

    # -- export -----------------------------------------------------------

    def all_details(self) -> list[PlaceDetail]:
        out = []
        for r in self.conn.execute("SELECT * FROM details"):
            d = PlaceDetail(
                fid=r["fid"], maps_url=r["maps_url"] or "", name=r["name"],
                cid=r["cid"], place_id=r["place_id"], website=r["website"],
                phone=r["phone"], address=r["address"], category=r["category"],
                rating=r["rating"], review_count=r["review_count"],
                plus_code=r["plus_code"], latitude=r["latitude"],
                longitude=r["longitude"],
                extraction_misses=[m for m in (r["misses"] or "").split(", ") if m],
            )
            try:
                d.website_tier = WebsiteTier(r["website_tier"])
            except ValueError:
                d.website_tier = WebsiteTier.NONE
            out.append(d)
        return out

    def tier_counts(self) -> dict[str, int]:
        return {
            r["website_tier"]: r["n"]
            for r in self.conn.execute(
                "SELECT website_tier, COUNT(*) n FROM details GROUP BY website_tier"
            )
        }

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT detail_state, COUNT(*) n FROM places GROUP BY detail_state"
        ).fetchall()
        return {r["detail_state"]: r["n"] for r in rows}
