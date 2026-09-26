"""SQLite storage — source of truth for discovered companies.

One row per canonical company (keyed by dedup_key). The full Pydantic model is
stored as JSON in `data`; a handful of columns are denormalised for cheap
filtering (status, tier, score, suppression). CSV export is the human surface;
this is the durable store.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .dedup import dedup_key, identity_keys, merge_companies, registrable_domain
from .models import Company

logger = logging.getLogger(__name__)

# The PK column keeps its historical name `dedup_key`, but its VALUE is the
# pinned `record_key`: assigned once on first insert and never recomputed, so a
# later phone/website enrichment can never orphan the row. k_domain/k_phone/k_name
# are the identity_keys() components, denormalised so an incoming company can be
# matched against an existing row by ANY shared identifier.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    dedup_key         TEXT PRIMARY KEY,
    domain            TEXT,
    name              TEXT,
    city              TEXT,
    province          TEXT,
    enrichment_status TEXT,
    priority_tier     TEXT,
    fit_score         INTEGER,
    draft_status      TEXT,
    is_suppressed     INTEGER,
    data              TEXT NOT NULL,
    first_seen        TEXT,
    last_seen         TEXT,
    k_domain          TEXT,
    k_phone           TEXT,
    k_name            TEXT
);
CREATE INDEX IF NOT EXISTS idx_enrichment ON companies(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_priority   ON companies(priority_tier);
CREATE INDEX IF NOT EXISTS idx_draft      ON companies(draft_status);
CREATE INDEX IF NOT EXISTS idx_k_domain   ON companies(k_domain);
CREATE INDEX IF NOT EXISTS idx_k_phone    ON companies(k_phone);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add identity-key columns to pre-existing DBs (CREATE TABLE won't)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col in ("k_domain", "k_phone", "k_name"):
        if col not in cols:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} TEXT")
    conn.commit()


def _write(conn: sqlite3.Connection, key: str, c: Company) -> None:
    ik = identity_keys(c)
    conn.execute(
        """
        INSERT INTO companies (dedup_key, domain, name, city, province,
            enrichment_status, priority_tier, fit_score,
            draft_status, is_suppressed, data, first_seen, last_seen,
            k_domain, k_phone, k_name)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(dedup_key) DO UPDATE SET
            domain=excluded.domain, name=excluded.name, city=excluded.city,
            province=excluded.province, enrichment_status=excluded.enrichment_status,
            priority_tier=excluded.priority_tier,
            fit_score=excluded.fit_score, draft_status=excluded.draft_status,
            is_suppressed=excluded.is_suppressed, data=excluded.data,
            first_seen=excluded.first_seen, last_seen=excluded.last_seen,
            k_domain=excluded.k_domain, k_phone=excluded.k_phone,
            k_name=excluded.k_name
        """,
        (
            key,
            c.domain,
            c.name,
            c.city,
            c.province,
            c.enrichment_status,
            c.priority_tier,
            c.fit_score,
            c.draft_status,
            1 if c.is_suppressed else 0,
            c.model_dump_json(),
            c.first_seen.isoformat(timespec="seconds"),
            c.last_seen.isoformat(timespec="seconds"),
            ik["domain"],
            ik["phone"],
            ik["name"],
        ),
    )


def upsert_company(conn: sqlite3.Connection, company: Company) -> str:
    """Insert or merge a company, matched by ANY shared identifier.

    Matches an incoming company against existing rows on domain, phone OR
    name+postcode, so the same company arriving via two sources (one with a
    website, one with only a phone) folds into a single row under the existing
    record_key. Returns the pinned key.

    An incoming record can match MORE than one row: it carries the website of
    one and the phone of another, which never shared an identifier with each
    other and so were never merged. `LIMIT 1` folded into whichever row the scan
    reached first and left the rest as permanent duplicates, so
    every match is folded into the oldest key and the others are deleted.
    """
    if not company.domain:
        company.domain = registrable_domain(company.website_url)
    ik = identity_keys(company)
    rows = conn.execute(
        """
        SELECT dedup_key FROM companies
        WHERE (k_domain = ? AND k_domain <> '')
           OR (k_phone  = ? AND k_phone  <> '')
           OR (k_name   = ? AND k_name   <> '')
        ORDER BY first_seen, dedup_key
        """,
        (ik["domain"], ik["phone"], ik["name"]),
    ).fetchall()
    if not rows:
        key = dedup_key(company)
        company.record_key = key
        _write(conn, key, company)
        conn.commit()
        return key

    # Oldest row wins the key, so the longest-lived identifier survives and the
    # choice is deterministic rather than whatever order the scan happened to
    # return. merge_companies keeps min(first_seen), so nothing loses its age.
    key = rows[0]["dedup_key"]
    merged = get_company(conn, key)
    absorbed = [r["dedup_key"] for r in rows[1:]]
    for other in absorbed:
        merged = merge_companies(merged, get_company(conn, other))
    merged = merge_companies(merged, company)
    merged.record_key = key
    _write(conn, key, merged)
    for other in absorbed:
        conn.execute("DELETE FROM companies WHERE dedup_key = ?", (other,))
    if absorbed:
        logger.info(
            f"dedup: {company.name!r} bridged {len(absorbed) + 1} rows; "
            f"folded {absorbed} into {key}"
        )
    conn.commit()
    return key


def save_company(conn: sqlite3.Connection, company: Company) -> str:
    """Overwrite an existing company in place (after enrich/score/draft).

    Writes under the pinned `record_key`; it never recomputes a key, so
    enrichment that adds a phone/website cannot orphan the row.
    """
    if not company.domain:
        company.domain = registrable_domain(company.website_url)
    key = company.record_key or dedup_key(company)
    company.record_key = key
    _write(conn, key, company)
    conn.commit()
    return key


def get_company(conn: sqlite3.Connection, key: str) -> Optional[Company]:
    row = conn.execute(
        "SELECT data FROM companies WHERE dedup_key = ?", (key,)
    ).fetchone()
    return Company.model_validate_json(row["data"]) if row else None


def iter_companies(
    conn: sqlite3.Connection,
    *,
    enrichment_status: Optional[str] = None,
    draft_status: Optional[str] = None,
    min_tier: Optional[str] = None,
    exclude_suppressed: bool = False,
    limit: Optional[int] = None,
) -> list[Company]:
    clauses, params = [], []
    if enrichment_status:
        clauses.append("enrichment_status = ?")
        params.append(enrichment_status)
    if draft_status:
        clauses.append("draft_status = ?")
        params.append(draft_status)
    if min_tier:
        allowed = {"A": ("A",), "B": ("A", "B"), "C": ("A", "B", "C")}[min_tier]
        clauses.append(f"priority_tier IN ({','.join('?' * len(allowed))})")
        params.extend(allowed)
    if exclude_suppressed:
        clauses.append("is_suppressed = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT data FROM companies {where} ORDER BY fit_score DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    return [Company.model_validate_json(r["data"]) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

    def group(col: str) -> dict:
        rows = conn.execute(
            f"SELECT {col}, COUNT(*) c FROM companies GROUP BY {col}"
        ).fetchall()
        return {(r[0] or "-"): r[1] for r in rows}

    with_email = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE data LIKE '%\"emails\":[{%'"
    ).fetchone()[0]
    return {
        "total": total,
        "by_enrichment": group("enrichment_status"),
        "by_priority_tier": group("priority_tier"),
        "by_draft_status": group("draft_status"),
        "with_email": with_email,
        "suppressed": conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_suppressed = 1"
        ).fetchone()[0],
    }


def upsert_many(conn: sqlite3.Connection, companies: Iterable[Company]) -> int:
    n = 0
    for c in companies:
        upsert_company(conn, c)
        n += 1
    return n
