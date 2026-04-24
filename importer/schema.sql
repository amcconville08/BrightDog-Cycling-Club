-- schema.sql — Garmin FIT import database
-- Located at: data/processed/sqlite/garmin_history.db

-- ── fit_files ─────────────────────────────────────────────────────────────
-- One row per unique file, keyed on SHA-256 hash.
-- Path is updated if the file moves; re-import is skipped if hash matches.
CREATE TABLE IF NOT EXISTS fit_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash    TEXT    NOT NULL UNIQUE,
    file_name    TEXT    NOT NULL,
    source_path  TEXT    NOT NULL,
    source       TEXT    NOT NULL,  -- 'garmin_export' | 'garmin_device'
    file_size_b  INTEGER NOT NULL,
    first_seen   TEXT    NOT NULL,  -- ISO-8601 UTC
    last_seen    TEXT    NOT NULL   -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_fit_files_hash   ON fit_files(file_hash);
CREATE INDEX IF NOT EXISTS idx_fit_files_source ON fit_files(source);


-- ── activity_metadata ─────────────────────────────────────────────────────
-- One row per successfully parsed FIT file.
-- All values come from the FIT session message — no time-series data.
CREATE TABLE IF NOT EXISTS activity_metadata (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fit_file_id     INTEGER NOT NULL REFERENCES fit_files(id),
    file_hash       TEXT    NOT NULL UNIQUE,

    -- Timing
    start_time      TEXT,           -- ISO-8601 (UTC)

    -- Activity type
    sport           TEXT,           -- e.g. 'cycling', 'running', 'generic'
    sub_sport       TEXT,           -- e.g. 'road', 'mountain', 'indoor_cycling'

    -- Volume
    duration_s      REAL,           -- total elapsed seconds
    distance_m      REAL,           -- metres
    total_ascent_m  REAL,           -- metres

    -- Physiology
    avg_heart_rate  INTEGER,        -- bpm
    max_heart_rate  INTEGER,        -- bpm

    -- Power
    avg_power       INTEGER,        -- watts
    max_power       INTEGER,        -- watts

    -- Cadence
    avg_cadence     INTEGER,        -- rpm

    -- Data presence flags
    has_hr          INTEGER NOT NULL DEFAULT 0,  -- 0 | 1
    has_power       INTEGER NOT NULL DEFAULT 0,
    has_gps         INTEGER NOT NULL DEFAULT 0,

    parsed_at       TEXT    NOT NULL  -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_activity_hash  ON activity_metadata(file_hash);
CREATE INDEX IF NOT EXISTS idx_activity_time  ON activity_metadata(start_time);
CREATE INDEX IF NOT EXISTS idx_activity_sport ON activity_metadata(sport);


-- ── import_errors ─────────────────────────────────────────────────────────
-- One row per file that could not be parsed.
CREATE TABLE IF NOT EXISTS import_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fit_file_id   INTEGER REFERENCES fit_files(id),
    file_hash     TEXT,
    source_path   TEXT    NOT NULL,
    error_type    TEXT,             -- exception class name
    error_message TEXT,
    occurred_at   TEXT    NOT NULL  -- ISO-8601 UTC
);
