-- schema_phase2.sql — Phase 2 additions to garmin_history.db
-- Applied with CREATE TABLE IF NOT EXISTS — safe to run on existing DB.
-- Phase 1 tables (fit_files, activity_metadata, import_errors) are unchanged.

-- ── activity_streams ──────────────────────────────────────────────────────
-- Full 1-Hz time-series from FIT record messages.
-- One row per second per activity (deduped on elapsed_s).
CREATE TABLE IF NOT EXISTS activity_streams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activity_metadata(id),
    elapsed_s   INTEGER NOT NULL,   -- seconds since activity start (≥ 0)
    timestamp   TEXT,               -- ISO-8601 UTC
    heart_rate  INTEGER,            -- bpm  (NULL if no HR monitor)
    power       INTEGER,            -- watts (NULL if no power meter; 0 = coasting)
    cadence     INTEGER,            -- rpm
    speed_ms    REAL,               -- m/s
    distance_m  REAL,               -- cumulative metres
    altitude_m  REAL,               -- metres (enhanced_altitude preferred)
    lat         REAL,               -- decimal degrees
    lon         REAL,               -- decimal degrees
    UNIQUE (activity_id, elapsed_s)
);

CREATE INDEX IF NOT EXISTS idx_streams_activity ON activity_streams(activity_id);
CREATE INDEX IF NOT EXISTS idx_streams_act_time ON activity_streams(activity_id, elapsed_s);


-- ── power_bests ───────────────────────────────────────────────────────────
-- Best mean-maximal power (MMP) for standard durations — sliding window.
-- Recomputed each time the activity's stream is (re)imported.
-- NULL = duration was shorter than the window, or no power data.
CREATE TABLE IF NOT EXISTS power_bests (
    activity_id INTEGER PRIMARY KEY REFERENCES activity_metadata(id),
    best_5s     INTEGER,    -- watts
    best_30s    INTEGER,
    best_1min   INTEGER,
    best_5min   INTEGER,
    best_10min  INTEGER,
    best_20min  INTEGER,
    best_60min  INTEGER,
    computed_at TEXT NOT NULL
);


-- ── activity_performance ──────────────────────────────────────────────────
-- Session-level derived metrics computed from the full time-series.
-- Replaces the session-summary values from Phase 1 with stream-derived ones.
CREATE TABLE IF NOT EXISTS activity_performance (
    activity_id           INTEGER PRIMARY KEY REFERENCES activity_metadata(id),

    -- Heart rate (stream-derived)
    hr_avg                INTEGER,   -- bpm
    hr_max                INTEGER,   -- bpm
    hr_drift_pct          REAL,      -- % rise first→second half (rides ≥ 60 min)

    -- Power (stream-derived, more accurate than session summary)
    power_avg             INTEGER,   -- watts (simple average)
    power_np              INTEGER,   -- normalised power (30s rolling, 4th-power law)
    power_max             INTEGER,   -- watts
    power_vi              REAL,      -- variability index = NP / avg_power

    -- Aerobic efficiency (Efficiency Factor)
    aerobic_efficiency    REAL,      -- avg_power / avg_hr  (W/bpm)

    -- FTP estimate from this activity
    ftp_candidate_w       INTEGER,   -- watts
    ftp_basis             TEXT,      -- '60min' | '20min×0.95' | NULL

    -- Stream coverage
    total_records         INTEGER,
    records_with_power    INTEGER,
    records_with_hr       INTEGER,
    power_quality_score   INTEGER,   -- 0–100: coverage × consistency

    -- Data presence flags (derived from stream, may differ from phase-1 metadata)
    has_power_stream      INTEGER NOT NULL DEFAULT 0,
    has_hr_stream         INTEGER NOT NULL DEFAULT 0,
    has_gps_stream        INTEGER NOT NULL DEFAULT 0,

    -- Athlete-specific power validation
    -- power_trusted = 0 when activity predates the accurate power meter install (2018-06-25).
    -- Raw stream data is preserved; this flag controls inclusion in modelling reports.
    power_trusted         INTEGER NOT NULL DEFAULT 1,
    power_exclusion_reason TEXT,

    computed_at           TEXT NOT NULL
);


-- ── timeseries_imports ────────────────────────────────────────────────────
-- Import status for Phase 2. One row per activity_metadata.id.
-- Prevents re-importing already processed activities on repeated runs.
CREATE TABLE IF NOT EXISTS timeseries_imports (
    activity_id   INTEGER PRIMARY KEY REFERENCES activity_metadata(id),
    status        TEXT NOT NULL,    -- 'done' | 'error' | 'skipped'
    record_count  INTEGER,          -- records inserted (0 if skipped)
    error_message TEXT,
    imported_at   TEXT NOT NULL
);
