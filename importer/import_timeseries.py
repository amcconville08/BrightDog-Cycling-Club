"""
import_timeseries.py — Phase 2: full FIT time-series import.

Reads record messages from targeted cycling activities, inserts per-second
stream data, and computes power bests + performance metrics in one pass.

Safe for repeated runs:
  - Activities already in timeseries_imports (status='done') are skipped.
  - INSERT OR IGNORE on (activity_id, elapsed_s) prevents duplicate records.
  - All writes are per-activity transactions — a crash mid-activity is harmless.

Usage:
  python importer/import_timeseries.py               # import all eligible
  python importer/import_timeseries.py --limit 10    # test on first 10
  python importer/import_timeseries.py --activity-id 42   # single activity
  python importer/import_timeseries.py --file path/to/file.fit  # by file
  python importer/import_timeseries.py --recompute-stats  # redo metrics only
  python importer/import_timeseries.py --force       # re-import even if done
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────
_THIS_DIR    = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent
DB_PATH      = PROJECT_ROOT / "data" / "processed" / "sqlite" / "garmin_history.db"
SCHEMA_P1    = _THIS_DIR / "schema.sql"
SCHEMA_P2    = _THIS_DIR / "schema_phase2.sql"

sys.path.insert(0, str(_THIS_DIR))

# ── Constants ─────────────────────────────────────────────────────────────
_SEMI_TO_DEG = 180.0 / (2 ** 31)       # FIT semicircle → decimal degrees
_MAX_POWER_W = 2500                     # sanity cap (spikes above discarded)
_MIN_DURATION_S = 300                   # exclude warmup stubs < 5 min
_MMP_DURATIONS = {                      # durations for mean-maximal power
    "best_5s":    5,
    "best_30s":   30,
    "best_1min":  60,
    "best_5min":  300,
    "best_10min": 600,
    "best_20min": 1200,
    "best_30min": 1800,
    "best_60min": 3600,
}

# ── Athlete-specific power validation — date cutoff ───────────────────────
# Before this date, multiple inaccurate/trial power meters were in use.
# Power data from these activities must not be used for FTP modelling,
# power curve analysis, or best-effort detection.
# Accurate power meter installed: 25 June 2018.
_POWER_TRUST_CUTOFF = "2018-06-25"   # ISO date string (exclusive lower bound)
_POWER_EXCLUSION_MSG = (
    "Pre-2018-06-25: inaccurate power meter period — "
    "data excluded from power modelling"
)

# ── Athlete-specific power ceiling rules (post-cutoff) ────────────────────
# Calibrated to known athlete history: true 20-min peak ~297 W,
# realistic FTP range 270–290 W. Values below are generous upper bounds
# beyond which data is almost certainly a trainer/calibration artifact.
#
# Rule A — 20-min best above physiological ceiling
_CEILING_20MIN_W    = 330
# Rule B — FTP estimate above ceiling
_CEILING_FTP_W      = 310
# Rule C — high 20-min power paired with suspiciously low HR
_SUSPICIOUS_20MIN_W = 320
# Rule D — high 60-min power paired with suspiciously low HR
_SUSPICIOUS_60MIN_W = 290
# Rules C/D — HR below this threshold at high power → likely trainer artefact
_SUSPICIOUS_LOW_HR  = 155
# Rule E — minimum power-data quality to enter the FTP model
_MIN_QUALITY_MODEL  = 50
# Rule F — sub_sport values that indicate indoor / virtual power sources
_INDOOR_SUB_SPORTS  = {"indoor_cycling", "virtual_activity", "spin"}

# Activities to target: sport matches or indoor cycling sub-sport
_CYCLING_WHERE = """
    (
        am.sport = 'cycling'
        OR (am.sub_sport = 'indoor_cycling')
    )
    AND am.start_time IS NOT NULL
    AND am.duration_s >= {min_dur}
""".format(min_dur=_MIN_DURATION_S)


# ── Database ──────────────────────────────────────────────────────────────

def _open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        print("Run: python importer/import_metadata.py", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-32000")    # 32 MB cache
    return conn


def _apply_schemas(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_P1.read_text())
    conn.executescript(SCHEMA_P2.read_text())
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Target selection ──────────────────────────────────────────────────────

def _get_targets(conn: sqlite3.Connection,
                 activity_id: Optional[int] = None,
                 file_path: Optional[str] = None,
                 force: bool = False) -> list:
    """
    Return list of activity dicts eligible for Phase 2 import.
    Excludes activities already in timeseries_imports unless --force.
    """
    base_sql = f"""
        SELECT am.id, am.file_hash, am.start_time, am.sport, am.sub_sport,
               am.duration_s, am.has_hr, am.has_power, am.has_gps,
               ff.source_path
        FROM activity_metadata am
        JOIN fit_files ff ON ff.file_hash = am.file_hash
        WHERE {_CYCLING_WHERE}
    """

    if activity_id is not None:
        sql = base_sql + f" AND am.id = {int(activity_id)}"
    elif file_path is not None:
        fp = str(Path(file_path).resolve())
        sql = base_sql + f" AND ff.source_path = '{fp}'"
    else:
        if not force:
            sql = base_sql + """
                AND am.id NOT IN (
                    SELECT activity_id FROM timeseries_imports
                    WHERE status = 'done'
                )
            """
        else:
            sql = base_sql

    sql += " ORDER BY am.start_time"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _import_status(conn: sqlite3.Connection, activity_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM timeseries_imports WHERE activity_id = ?",
        (activity_id,)
    ).fetchone()
    return row["status"] if row else None


def _mark_import(conn: sqlite3.Connection, activity_id: int,
                 status: str, count: int = 0, error: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO timeseries_imports
           (activity_id, status, record_count, error_message, imported_at)
           VALUES (?, ?, ?, ?, ?)""",
        (activity_id, status, count, error or None, _now()),
    )


# ── FIT record parsing ────────────────────────────────────────────────────

def _parse_records(source_path: str, start_time_iso: str) -> list:
    """
    Parse all record messages from a FIT file.

    Returns a list of dicts sorted by elapsed_s. Deduplicates on elapsed_s
    (last record wins). Silently tolerates CRC errors and truncated files.
    """
    from fitparse import FitFile

    start_dt = datetime.fromisoformat(start_time_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    by_elapsed: dict = {}

    try:
        fit = FitFile(source_path)
        for msg in fit.get_messages("record"):
            try:
                d = msg.get_values()
            except Exception:
                continue

            ts = d.get("timestamp")
            if ts is None:
                continue

            # Normalise to UTC-aware datetime
            if not hasattr(ts, "isoformat"):
                continue
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)

            elapsed_s = int(round((ts - start_dt).total_seconds()))
            if elapsed_s < 0 or elapsed_s > 86_400:   # max 24-hour activity
                continue

            # GPS: semicircles → decimal degrees
            lat = d.get("position_lat")
            lon = d.get("position_long")
            if lat is not None:
                lat = round(lat * _SEMI_TO_DEG, 6)
            if lon is not None:
                lon = round(lon * _SEMI_TO_DEG, 6)

            # Altitude: prefer enhanced
            altitude = d.get("enhanced_altitude") or d.get("altitude")

            # Power: cap obvious spikes
            power = d.get("power")
            if power is not None:
                power = int(power)
                if power > _MAX_POWER_W:
                    power = None

            by_elapsed[elapsed_s] = {
                "elapsed_s":  elapsed_s,
                "timestamp":  ts.isoformat(),
                "heart_rate": d.get("heart_rate"),
                "power":      power,
                "cadence":    d.get("cadence"),
                "speed_ms":   d.get("speed"),
                "distance_m": d.get("distance"),
                "altitude_m": altitude,
                "lat":        lat,
                "lon":        lon,
            }

    except Exception:
        pass  # CRC mismatch, truncated file — use whatever was parsed

    return sorted(by_elapsed.values(), key=lambda r: r["elapsed_s"])


# ── Signal analysis helpers ───────────────────────────────────────────────

def _build_1hz(records: list, field: str) -> list:
    """
    Resample a field from sparse records to a 1-Hz array.

    Gaps are forward-filled (last-value-carried-forward), which is the
    standard approach for smart-recording FIT files. The first seconds
    before any valid value remain None.
    """
    if not records:
        return []
    max_t = records[-1]["elapsed_s"]
    arr = [None] * (max_t + 1)
    for r in records:
        v = r.get(field)
        if v is not None:
            arr[r["elapsed_s"]] = v

    # Forward fill
    last = None
    filled = []
    for v in arr:
        if v is not None:
            last = v
        filled.append(last)
    return filled


def _best_power_window(p_1hz: list, window_s: int) -> Optional[int]:
    """
    Best mean-maximal power over a sliding window of window_s seconds.
    None values are treated as 0 (industry standard for MMP calculation).
    Returns None if the activity is shorter than the window.
    """
    n = len(p_1hz)
    if n < window_s:
        return None

    # Replace None with 0
    p = [v if v is not None else 0 for v in p_1hz]

    w = sum(p[:window_s])
    best = w

    for i in range(window_s, n):
        w += p[i] - p[i - window_s]
        if w > best:
            best = w

    return int(round(best / window_s))


def _compute_np(p_1hz: list) -> Optional[int]:
    """
    Normalised Power: 30-second rolling average raised to the 4th power,
    averaged, then the 4th root taken.
    Returns None for activities shorter than 30 seconds.
    """
    n = len(p_1hz)
    if n < 30:
        return None

    p = [v if v is not None else 0 for v in p_1hz]

    # 30-second rolling average (O(n) sliding window)
    w = sum(p[:30])
    rolling_4th_sum = 0.0
    count = n - 29

    for i in range(30, n):
        avg = w / 30
        rolling_4th_sum += avg ** 4
        w += p[i] - p[i - 30]

    # Include the last window
    rolling_4th_sum += (w / 30) ** 4

    mean_4th = rolling_4th_sum / count
    return int(round(mean_4th ** 0.25))


def _compute_power_bests(records: list) -> dict:
    """Compute best MMP for all standard durations from the record stream."""
    p_1hz = _build_1hz(records, "power")

    # Only compute if there's meaningful power data
    valid_count = sum(1 for v in p_1hz if v is not None and v > 0)
    if valid_count < 10:
        return {k: None for k in _MMP_DURATIONS}

    return {
        col: _best_power_window(p_1hz, secs)
        for col, secs in _MMP_DURATIONS.items()
    }


def _check_suspicious_power(
    bests: dict,
    ftp_candidate_w: Optional[int],
    hr_avg: Optional[int],
    quality: int,
    sub_sport: str,
    power_trusted: int,
) -> tuple:
    """
    Evaluate post-cutoff trusted power data for physiological plausibility.

    Returns (is_suspicious: int, reason: str|None, include_in_ftp_model: int).

    Rules (any one is sufficient to flag suspicious):
      A  20-min best > _CEILING_20MIN_W
      B  FTP estimate > _CEILING_FTP_W
      C  20-min best > _SUSPICIOUS_20MIN_W  AND  avg HR < _SUSPICIOUS_LOW_HR
      D  60-min best > _SUSPICIOUS_60MIN_W  AND  avg HR < _SUSPICIOUS_LOW_HR
      E  power_quality_score < _MIN_QUALITY_MODEL
      F  indoor/virtual sub_sport  AND  at least one other rule fires

    Untrusted activities (power_trusted = 0) are already excluded;
    this function returns a neutral result for them.
    """
    if not power_trusted:
        # Already excluded by the date cutoff — don't double-flag
        return 0, None, 0

    # No power data at all — not suspicious, just no meter fitted.
    # FTP reports already filter on ftp_candidate_w IS NOT NULL so these
    # activities are naturally excluded from power modelling without needing
    # a suspicious flag.
    if quality == 0 and not ftp_candidate_w:
        return 0, None, 1

    reasons = []

    best_20 = bests.get("best_20min") or 0
    best_60 = bests.get("best_60min") or 0
    ftp_est = ftp_candidate_w or 0
    hr      = hr_avg or 999   # treat missing HR as high (won't trigger low-HR rules)

    # Rule A
    if best_20 > _CEILING_20MIN_W:
        reasons.append(
            f"20-min best {best_20}W exceeds athlete ceiling ({_CEILING_20MIN_W}W)"
        )

    # Rule B
    if ftp_est > _CEILING_FTP_W:
        reasons.append(
            f"FTP estimate {ftp_est}W exceeds athlete ceiling ({_CEILING_FTP_W}W)"
        )

    # Rule C
    if best_20 > _SUSPICIOUS_20MIN_W and hr < _SUSPICIOUS_LOW_HR:
        reasons.append(
            f"20-min best {best_20}W with avg HR {hr}bpm "
            f"(high power + low HR — probable trainer scaling artefact)"
        )

    # Rule D
    if best_60 > _SUSPICIOUS_60MIN_W and hr < _SUSPICIOUS_LOW_HR:
        reasons.append(
            f"60-min best {best_60}W with avg HR {hr}bpm "
            f"(high 60-min power + low HR — probable trainer scaling artefact)"
        )

    # Rule E — only applies when there IS power data but coverage is patchy.
    # quality == 0 means no power meter at all; that's handled above.
    if 0 < quality < _MIN_QUALITY_MODEL:
        reasons.append(
            f"Power quality score {quality}/100 below minimum ({_MIN_QUALITY_MODEL})"
        )

    # Rule F — indoor/virtual flag (only as co-reason, not standalone)
    if sub_sport.lower() in _INDOOR_SUB_SPORTS and reasons:
        reasons.append(f"Indoor/virtual activity (sub_sport={sub_sport!r})")

    is_suspicious = 1 if reasons else 0
    reason_str    = "; ".join(reasons) if reasons else None
    include       = 0 if is_suspicious else 1

    return is_suspicious, reason_str, include


def _compute_performance(records: list, activity: dict, bests: dict) -> dict:
    """
    Derive session-level metrics from the full record stream.

    Returns a dict matching the activity_performance schema columns.
    """
    total = len(records)
    if total == 0:
        return {}

    hr_vals  = [r["heart_rate"] for r in records if r.get("heart_rate") is not None]
    pwr_vals = [r["power"]      for r in records if r.get("power")      is not None and r["power"] > 0]
    lat_vals = [r["lat"]        for r in records if r.get("lat")        is not None]

    # ── Heart rate ────────────────────────────────────────────────────────
    hr_avg = int(round(sum(hr_vals) / len(hr_vals))) if hr_vals else None
    hr_max = max(hr_vals)                              if hr_vals else None

    # HR drift — only meaningful for rides ≥ 60 min
    hr_drift = None
    duration_s = float(activity.get("duration_s") or 0)
    if hr_vals and duration_s >= 3600:
        mid = len(hr_vals) // 2
        avg1 = sum(hr_vals[:mid]) / len(hr_vals[:mid]) if hr_vals[:mid] else None
        avg2 = sum(hr_vals[mid:]) / len(hr_vals[mid:]) if hr_vals[mid:] else None
        if avg1 and avg2 and avg1 > 0:
            hr_drift = round((avg2 - avg1) / avg1 * 100, 2)

    # ── Power ─────────────────────────────────────────────────────────────
    power_avg = int(round(sum(pwr_vals) / len(pwr_vals))) if pwr_vals else None
    power_max = max(pwr_vals)                              if pwr_vals else None
    p_1hz     = _build_1hz(records, "power")
    power_np  = _compute_np(p_1hz) if pwr_vals else None
    power_vi  = round(power_np / power_avg, 3) if (power_np and power_avg) else None

    # ── Aerobic efficiency ────────────────────────────────────────────────
    ae = round(power_avg / hr_avg, 3) if (power_avg and hr_avg) else None

    # ── FTP candidate ─────────────────────────────────────────────────────
    ftp_w, ftp_basis = None, None
    if bests.get("best_60min"):
        ftp_w    = bests["best_60min"]
        ftp_basis = "60min"
    elif bests.get("best_20min"):
        ftp_w    = int(round(bests["best_20min"] * 0.95))
        ftp_basis = "20min×0.95"

    # ── Power data quality ────────────────────────────────────────────────
    # Score = % of records with non-zero power
    quality = int(round(len(pwr_vals) / total * 100)) if total else 0

    # ── Power trust flag (date cutoff) ───────────────────────────────────
    # Activities before 2018-06-25 used inaccurate/trial power meters.
    # Mark as untrusted so reports can exclude them from modelling.
    # HR data, metadata, and distance/duration remain valid.
    activity_date = (activity.get("start_time") or "")[:10]
    if activity_date and activity_date < _POWER_TRUST_CUTOFF:
        power_trusted = 0
        power_exclusion_reason = _POWER_EXCLUSION_MSG
    else:
        power_trusted = 1
        power_exclusion_reason = None

    # ── Suspicious power detection (post-cutoff activities only) ─────────
    # Even within the trusted period, trainer calibration errors and virtual
    # power artefacts can produce physiologically implausible readings.
    # Flagging here keeps raw values intact while excluding from FTP model.
    is_suspicious, suspicious_reason, include_in_ftp = _check_suspicious_power(
        bests=bests,
        ftp_candidate_w=ftp_w,
        hr_avg=hr_avg,
        quality=quality,
        sub_sport=(activity.get("sub_sport") or ""),
        power_trusted=power_trusted,
    )

    return {
        "hr_avg":                 hr_avg,
        "hr_max":                 hr_max,
        "hr_drift_pct":           hr_drift,
        "power_avg":              power_avg,
        "power_np":               power_np,
        "power_max":              power_max,
        "power_vi":               power_vi,
        "aerobic_efficiency":     ae,
        "ftp_candidate_w":        ftp_w,
        "ftp_basis":              ftp_basis,
        "total_records":          total,
        "records_with_power":     len(pwr_vals),
        "records_with_hr":        len(hr_vals),
        "power_quality_score":    quality,
        "has_power_stream":       1 if pwr_vals else 0,
        "has_hr_stream":          1 if hr_vals  else 0,
        "has_gps_stream":         1 if lat_vals else 0,
        "power_trusted":          power_trusted,
        "power_exclusion_reason": power_exclusion_reason,
        "is_suspicious_power":    is_suspicious,
        "suspicious_reason":      suspicious_reason,
        "include_in_ftp_model":   include_in_ftp,
    }


# ── Database writes ───────────────────────────────────────────────────────

def _insert_streams(conn: sqlite3.Connection, activity_id: int, records: list) -> None:
    rows = [
        (
            activity_id,
            r["elapsed_s"],   r["timestamp"],
            r.get("heart_rate"),  r.get("power"),
            r.get("cadence"),     r.get("speed_ms"),
            r.get("distance_m"),  r.get("altitude_m"),
            r.get("lat"),         r.get("lon"),
        )
        for r in records
    ]
    conn.executemany(
        """INSERT OR IGNORE INTO activity_streams
           (activity_id, elapsed_s, timestamp, heart_rate, power, cadence,
            speed_ms, distance_m, altitude_m, lat, lon)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )


def _upsert_power_bests(conn: sqlite3.Connection,
                         activity_id: int, bests: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO power_bests
           (activity_id, best_5s, best_30s, best_1min,
            best_5min, best_10min, best_20min, best_30min, best_60min, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            activity_id,
            bests.get("best_5s"),    bests.get("best_30s"),
            bests.get("best_1min"),  bests.get("best_5min"),
            bests.get("best_10min"), bests.get("best_20min"),
            bests.get("best_30min"), bests.get("best_60min"),
            _now(),
        ),
    )


def _upsert_performance(conn: sqlite3.Connection,
                         activity_id: int, perf: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO activity_performance
           (activity_id, hr_avg, hr_max, hr_drift_pct,
            power_avg, power_np, power_max, power_vi,
            aerobic_efficiency, ftp_candidate_w, ftp_basis,
            total_records, records_with_power, records_with_hr,
            power_quality_score,
            has_power_stream, has_hr_stream, has_gps_stream,
            power_trusted, power_exclusion_reason,
            is_suspicious_power, suspicious_reason, include_in_ftp_model,
            computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            activity_id,
            perf.get("hr_avg"),         perf.get("hr_max"),
            perf.get("hr_drift_pct"),
            perf.get("power_avg"),      perf.get("power_np"),
            perf.get("power_max"),      perf.get("power_vi"),
            perf.get("aerobic_efficiency"),
            perf.get("ftp_candidate_w"), perf.get("ftp_basis"),
            perf.get("total_records"),
            perf.get("records_with_power"), perf.get("records_with_hr"),
            perf.get("power_quality_score"),
            perf.get("has_power_stream", 0),
            perf.get("has_hr_stream",    0),
            perf.get("has_gps_stream",   0),
            perf.get("power_trusted",         1),
            perf.get("power_exclusion_reason"),
            perf.get("is_suspicious_power",   0),
            perf.get("suspicious_reason"),
            perf.get("include_in_ftp_model",  1),
            _now(),
        ),
    )


# ── Main ──────────────────────────────────────────────────────────────────

def run(limit: int = 0,
        activity_id: Optional[int] = None,
        file_path: Optional[str] = None,
        verbose: bool = False,
        force: bool = False,
        recompute_stats: bool = False) -> None:

    print(f"Project root : {PROJECT_ROOT}")
    print(f"Database     : {DB_PATH}")
    print()

    conn = _open_db()
    _apply_schemas(conn)

    targets = _get_targets(conn, activity_id=activity_id,
                           file_path=file_path, force=force)
    total = len(targets)
    print(f"Target activities: {total}")
    if limit:
        targets = targets[:limit]
        print(f"(Limited to {limit} for this run)")
    print()

    stats = {"done": 0, "skipped": 0, "errors": 0, "records": 0}

    for i, act in enumerate(targets, 1):
        act_id  = act["id"]
        src     = act["source_path"]
        short   = os.path.relpath(src, PROJECT_ROOT)
        date    = (act.get("start_time") or "?")[:10]
        dur_min = int((act.get("duration_s") or 0) / 60)

        # ── Skip check ────────────────────────────────────────────────────
        if not force and not recompute_stats:
            existing = _import_status(conn, act_id)
            if existing == "done":
                stats["skipped"] += 1
                continue

        if verbose or (i % 25 == 0) or i == len(targets):
            print(f"  [{i:>4}/{len(targets)}] {date}  {dur_min:>4}min  {short}")

        # ── Recompute-only mode: skip stream import ───────────────────────
        if recompute_stats:
            records = conn.execute(
                """SELECT elapsed_s, timestamp, heart_rate, power, cadence,
                          speed_ms, distance_m, altitude_m, lat, lon
                   FROM activity_streams WHERE activity_id = ?
                   ORDER BY elapsed_s""",
                (act_id,),
            ).fetchall()
            records = [dict(r) for r in records]

            if not records:
                if verbose:
                    print(f"    [skip] no stream data yet — run without --recompute-stats first")
                continue

            bests = _compute_power_bests(records)
            perf  = _compute_performance(records, act, bests)
            _upsert_power_bests(conn, act_id, bests)
            _upsert_performance(conn, act_id, perf)
            conn.commit()
            stats["done"] += 1
            continue

        # ── Full stream import ────────────────────────────────────────────
        try:
            records = _parse_records(src, act["start_time"])

            if not records:
                _mark_import(conn, act_id, "skipped", 0, "no record messages")
                stats["skipped"] += 1
                conn.commit()
                continue

            _insert_streams(conn, act_id, records)

            bests = _compute_power_bests(records)
            perf  = _compute_performance(records, act, bests)

            _upsert_power_bests(conn, act_id, bests)
            _upsert_performance(conn, act_id, perf)
            _mark_import(conn, act_id, "done", len(records))

            conn.commit()

            stats["done"]    += 1
            stats["records"] += len(records)

            if verbose:
                flags = (
                    f"[{'P' if perf.get('has_power_stream') else '-'}"
                    f"{'H' if perf.get('has_hr_stream')    else '-'}"
                    f"{'G' if perf.get('has_gps_stream')   else '-'}]"
                )
                np_str  = f"NP {perf['power_np']}W"    if perf.get("power_np")  else ""
                ftp_str = f"FTP~ {perf['ftp_candidate_w']}W" if perf.get("ftp_candidate_w") else ""
                print(f"    → {len(records):>5} records  {flags}  {np_str}  {ftp_str}")

        except Exception as exc:
            err = str(exc)[:300]
            print(f"  [ERROR] {short}: {err}", file=sys.stderr)
            _mark_import(conn, act_id, "error", 0, err)
            conn.commit()
            stats["errors"] += 1

    # ── Summary ───────────────────────────────────────────────────────────
    print()
    print("─" * 50)
    print(f"  Imported      : {stats['done']}")
    print(f"  Skipped       : {stats['skipped']}")
    print(f"  Errors        : {stats['errors']}")
    print(f"  Records total : {stats['records']:,}")
    print("─" * 50)

    total_streams = conn.execute(
        "SELECT COUNT(*) FROM activity_streams"
    ).fetchone()[0]
    total_done = conn.execute(
        "SELECT COUNT(*) FROM timeseries_imports WHERE status='done'"
    ).fetchone()[0]
    print(f"  DB total — stream rows: {total_streams:,}  activities done: {total_done}")
    print()

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 2: import full FIT time-series for cycling activities"
    )
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="Process at most N activities (0 = all)")
    parser.add_argument("--activity-id", type=int, default=None,
                        help="Import a single activity by activity_metadata.id")
    parser.add_argument("--file", type=str, default=None,
                        help="Import by source FIT file path (must be in phase-1 DB)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-activity detail")
    parser.add_argument("--force", action="store_true",
                        help="Re-import activities already marked done")
    parser.add_argument("--recompute-stats", action="store_true",
                        help="Re-derive power bests + metrics from existing streams "
                             "(no stream re-import)")
    args = parser.parse_args()
    run(
        limit=args.limit,
        activity_id=args.activity_id,
        file_path=args.file,
        verbose=args.verbose,
        force=args.force,
        recompute_stats=args.recompute_stats,
    )
