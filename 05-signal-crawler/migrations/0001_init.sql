-- 0001_init.sql — core schema for the signal crawler.
-- All tables here. Indexes (beyond UNIQUE-implied ones) live in 0002_indexes.sql.

CREATE TABLE sources (
    name         TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 1,
    status       TEXT    NOT NULL DEFAULT 'ok',   -- ok | needs_review | paused
    rate_per_min INTEGER NOT NULL
);

CREATE TABLE cursors (
    source       TEXT PRIMARY KEY REFERENCES sources(name) ON DELETE CASCADE,
    page         INTEGER,
    max_id       TEXT,
    processed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,
    cursor      TEXT,
    status      TEXT    NOT NULL CHECK(status IN ('pending','running','done','failed')),
    claimed_by  TEXT,
    claimed_at  DATETIME,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error_msg   TEXT,
    run_after   DATETIME,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE inbox (
    id              INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL,
    source_item_id  TEXT    NOT NULL,
    url             TEXT,
    raw_content     TEXT    NOT NULL,
    hash            TEXT    NOT NULL,
    scraped_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    extracted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, source_item_id)
);

CREATE TABLE signals (
    id            INTEGER PRIMARY KEY,
    inbox_id      INTEGER NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
    signal_type   TEXT    NOT NULL,   -- alt_to|replace|instead|moving|shutdown|dead|disc|rip|eol
    signal_text   TEXT    NOT NULL,
    mentioned_app TEXT    NOT NULL,
    confidence    REAL    NOT NULL,
    UNIQUE(inbox_id, signal_type, signal_text)
);

CREATE TABLE apps (
    name                  TEXT    NOT NULL,
    platform              TEXT    NOT NULL DEFAULT 'unknown',
    score                 REAL    NOT NULL DEFAULT 0,
    mention_count         INTEGER NOT NULL DEFAULT 0,
    source_count          INTEGER NOT NULL DEFAULT 0,
    alttto_discontinued   INTEGER NOT NULL DEFAULT 0,
    last_seen             DATETIME,
    PRIMARY KEY (name, platform)
);

CREATE TABLE inbox_parse_error (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,
    url         TEXT,
    html_hash   TEXT,
    detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason      TEXT
);

CREATE TABLE control (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE runs (
    id             INTEGER PRIMARY KEY,
    started_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at       DATETIME,
    items_scraped  INTEGER  NOT NULL DEFAULT 0,
    errors_count   INTEGER  NOT NULL DEFAULT 0
);
