"""
generate_phase2_reports.py — Phase 2 CSV reports from the performance database.

Usage:
    python importer/generate_phase2_reports.py

Output (data/reports/):
    power_curve_by_year.csv             Best MMP per duration per year (model-valid only)
    best_efforts_by_year.csv            Top-3 efforts per duration per year (model-valid only)
    aerobic_efficiency_by_year.csv      Efficiency Factor trend by year (all HR activities)
    power_data_quality.csv              Quality score per power activity
    candidate_ftp_history_filtered.csv  FTP estimates in date order (model-valid only)
    suspicious_power_efforts.csv        Flagged efforts excluded from modelling + reasons
    top_valid_threshold_efforts.csv     Best model-valid threshold rides ranked by FTP

Athlete-specific validation:
    Gate 1 — date cutoff (power_trusted):
        Power data before 2018-06-25 excluded. Multiple inaccurate/trial
        power meters in use before accurate meter installed 25 June 2018.
    Gate 2 — physiological plausibility (is_suspicious_power):
        Post-cutoff activities are further checked against athlete-specific
        ceilings and HR cross-checks to catch trainer scaling artefacts.
    Combined gate — include_in_ftp_model = 1:
        Only activities passing BOTH gates enter power modelling reports.
    HR data, ride metadata, and aerobic efficiency are included for all dates.
"""
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR    = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent
DB_PATH      = PROJECT_ROOT / "data" / "processed" / "sqlite" / "garmin_history.db"
REPORTS_DIR  = PROJECT_ROOT / "data" / "reports"

_MMP_COLS = ["best_5s", "best_30s", "best_1min",
             "best_5min", "best_10min", "best_20min", "best_30min", "best_60min"]

_POWER_TRUST_CUTOFF = "2018-06-25"   # matches import_timeseries.py constant


# ── Helpers ───────────────────────────────────────────────────────────────

def _open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _write_csv(name: str, rows: list, fields: list) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  Written: {os.path.relpath(path, PROJECT_ROOT)}  ({len(rows)} rows)")


def _year(iso: str | None) -> str | None:
    return iso[:4] if iso else None


# ── Report 1: power_curve_by_year ─────────────────────────────────────────

def power_curve_by_year(conn: sqlite3.Connection) -> None:
    """
    Best MMP for each standard duration, grouped by year.
    Filtered to include_in_ftp_model = 1 (trusted + not suspicious).
    """
    col_list = ", ".join(f"MAX(pb.{c}) AS {c}" for c in _MMP_COLS)
    rows = conn.execute(f"""
        SELECT
            SUBSTR(am.start_time, 1, 4)         AS year,
            COUNT(DISTINCT pb.activity_id)       AS activities_with_power,
            {col_list}
        FROM power_bests pb
        JOIN activity_metadata am ON am.id = pb.activity_id
        JOIN activity_performance ap ON ap.activity_id = pb.activity_id
        WHERE am.start_time IS NOT NULL
          AND ap.include_in_ftp_model = 1
        GROUP BY year
        ORDER BY year
    """).fetchall()

    out = [dict(r) for r in rows]
    fields = ["year", "activities_with_power"] + _MMP_COLS
    _write_csv("power_curve_by_year.csv", out, fields)


# ── Report 2: best_efforts_by_year ────────────────────────────────────────

def best_efforts_by_year(conn: sqlite3.Connection) -> None:
    """
    Top-3 activities for each standard duration, per year.
    Filtered to include_in_ftp_model = 1 only.
    """
    out = []
    for col in _MMP_COLS:
        dur_label = col.replace("best_", "")
        rows = conn.execute(f"""
            SELECT
                SUBSTR(am.start_time, 1, 4) AS year,
                am.start_time,
                am.duration_s,
                pb.{col}                    AS watts,
                ap.power_quality_score      AS quality,
                ff.source_path
            FROM power_bests pb
            JOIN activity_metadata am  ON am.id  = pb.activity_id
            JOIN activity_performance ap ON ap.activity_id = pb.activity_id
            JOIN fit_files ff           ON ff.file_hash = am.file_hash
            WHERE pb.{col} IS NOT NULL
              AND am.start_time IS NOT NULL
              AND ap.include_in_ftp_model = 1
            ORDER BY year, pb.{col} DESC
        """).fetchall()

        seen: dict = {}
        for r in rows:
            yr = r["year"]
            if yr not in seen:
                seen[yr] = 0
            if seen[yr] < 3:
                out.append({
                    "duration":     dur_label,
                    "year":         yr,
                    "rank":         seen[yr] + 1,
                    "date":         (r["start_time"] or "")[:10],
                    "watts":        r["watts"],
                    "quality_pct":  r["quality"],
                    "activity_min": int((r["duration_s"] or 0) / 60),
                })
                seen[yr] += 1

    fields = ["duration", "year", "rank", "date", "watts",
              "activity_min", "quality_pct"]
    _write_csv("best_efforts_by_year.csv", out, fields)


# ── Report 3: aerobic_efficiency_by_year ──────────────────────────────────

def aerobic_efficiency_by_year(conn: sqlite3.Connection) -> None:
    """
    Efficiency Factor (EF = avg_power / avg_hr) trend by year.
    All rides ≥ 30 min with HR data — not filtered by power trust,
    since aerobic trends remain meaningful even with inaccurate power.
    """
    rows = conn.execute("""
        SELECT
            SUBSTR(am.start_time, 1, 4)             AS year,
            COUNT(*)                                 AS activities,
            ROUND(AVG(ap.aerobic_efficiency), 3)     AS ef_avg,
            ROUND(MAX(ap.aerobic_efficiency), 3)     AS ef_best,
            ROUND(MIN(ap.aerobic_efficiency), 3)     AS ef_worst,
            ROUND(AVG(ap.hr_drift_pct), 2)           AS avg_hr_drift_pct,
            ROUND(AVG(ap.power_avg), 0)              AS avg_power_w,
            ROUND(AVG(ap.hr_avg), 0)                 AS avg_hr_bpm
        FROM activity_performance ap
        JOIN activity_metadata am ON am.id = ap.activity_id
        WHERE ap.aerobic_efficiency IS NOT NULL
          AND am.start_time IS NOT NULL
          AND am.duration_s >= 1800     -- at least 30 min for meaningful EF
        GROUP BY year
        ORDER BY year
    """).fetchall()

    fields = ["year", "activities", "ef_avg", "ef_best", "ef_worst",
              "avg_hr_drift_pct", "avg_power_w", "avg_hr_bpm"]
    _write_csv("aerobic_efficiency_by_year.csv", [dict(r) for r in rows], fields)


# ── Report 4: power_data_quality ─────────────────────────────────────────

def power_data_quality(conn: sqlite3.Connection) -> None:
    """
    Per-activity power data quality report — all power activities regardless
    of trust/suspicious status. Includes flags so exclusions are visible.
    """
    rows = conn.execute("""
        SELECT
            am.start_time,
            am.sport,
            am.sub_sport,
            ROUND(am.duration_s / 60.0, 1)      AS duration_min,
            ap.total_records,
            ap.records_with_power,
            ap.power_quality_score,
            ap.power_avg,
            ap.power_np,
            ap.power_vi,
            ap.ftp_candidate_w,
            ap.ftp_basis,
            ap.power_trusted,
            ap.is_suspicious_power,
            ap.include_in_ftp_model,
            ff.source_path
        FROM activity_performance ap
        JOIN activity_metadata am ON am.id = ap.activity_id
        JOIN fit_files ff          ON ff.file_hash = am.file_hash
        WHERE ap.has_power_stream = 1
          AND am.start_time IS NOT NULL
        ORDER BY ap.power_quality_score DESC, am.start_time DESC
    """).fetchall()

    out = [dict(r) for r in rows]
    for row in out:
        row["date"] = (row.pop("start_time") or "")[:10]
        row["source_path"] = os.path.relpath(row["source_path"], PROJECT_ROOT)

    fields = ["date", "sport", "sub_sport", "duration_min",
              "total_records", "records_with_power", "power_quality_score",
              "power_avg", "power_np", "power_vi",
              "ftp_candidate_w", "ftp_basis",
              "power_trusted", "is_suspicious_power", "include_in_ftp_model",
              "source_path"]
    _write_csv("power_data_quality.csv", out, fields)


# ── Report 5: candidate_ftp_history_filtered ──────────────────────────────

def candidate_ftp_history(conn: sqlite3.Connection) -> None:
    """
    Chronological FTP estimates from model-valid activities only.
    include_in_ftp_model = 1 enforces both the date cutoff and plausibility gates.

    Methodology:
      - If best_60min exists → FTP candidate = best_60min W
      - If best_20min exists → FTP candidate = best_20min × 0.95
    """
    rows = conn.execute("""
        SELECT
            am.start_time,
            am.sport,
            ROUND(am.duration_s / 60.0, 1)   AS duration_min,
            ap.ftp_candidate_w,
            ap.ftp_basis,
            ap.power_quality_score,
            ap.power_np,
            pb.best_20min,
            pb.best_30min,
            pb.best_60min
        FROM activity_performance ap
        JOIN activity_metadata am  ON am.id = ap.activity_id
        JOIN power_bests pb         ON pb.activity_id = ap.activity_id
        WHERE ap.ftp_candidate_w IS NOT NULL
          AND ap.include_in_ftp_model = 1
          AND am.start_time IS NOT NULL
          AND am.duration_s >= 1200        -- at least 20 min required
        ORDER BY am.start_time
    """).fetchall()

    out = []
    for r in rows:
        out.append({
            "date":                (r["start_time"] or "")[:10],
            "sport":               r["sport"],
            "duration_min":        r["duration_min"],
            "ftp_candidate_w":     r["ftp_candidate_w"],
            "ftp_basis":           r["ftp_basis"],
            "best_20min_w":        r["best_20min"],
            "best_30min_w":        r["best_30min"],
            "best_60min_w":        r["best_60min"],
            "normalised_power_w":  r["power_np"],
            "quality_score":       r["power_quality_score"],
        })

    fields = ["date", "sport", "duration_min", "ftp_candidate_w", "ftp_basis",
              "best_20min_w", "best_30min_w", "best_60min_w",
              "normalised_power_w", "quality_score"]
    _write_csv("candidate_ftp_history_filtered.csv", out, fields)


# ── Report 6: suspicious_power_efforts ───────────────────────────────────

def suspicious_power_efforts(conn: sqlite3.Connection) -> None:
    """
    All trusted-period activities flagged as physiologically suspicious.
    These are excluded from FTP modelling. Report preserved for review.
    """
    rows = conn.execute("""
        SELECT
            am.start_time,
            am.sport,
            am.sub_sport,
            ROUND(am.duration_s / 60.0, 1)   AS duration_min,
            ap.ftp_candidate_w,
            ap.ftp_basis,
            ap.power_quality_score,
            ap.hr_avg,
            ap.hr_max,
            ap.power_avg,
            ap.power_np,
            pb.best_20min,
            pb.best_30min,
            pb.best_60min,
            ap.suspicious_reason,
            ff.source_path
        FROM activity_performance ap
        JOIN activity_metadata am ON am.id = ap.activity_id
        JOIN power_bests pb        ON pb.activity_id = ap.activity_id
        JOIN fit_files ff          ON ff.file_hash = am.file_hash
        WHERE ap.power_trusted = 1
          AND ap.is_suspicious_power = 1
          AND am.start_time IS NOT NULL
        ORDER BY ap.ftp_candidate_w DESC NULLS LAST
    """).fetchall()

    out = []
    for r in rows:
        out.append({
            "date":              (r["start_time"] or "")[:10],
            "sport":             r["sport"],
            "sub_sport":         r["sub_sport"],
            "duration_min":      r["duration_min"],
            "ftp_candidate_w":   r["ftp_candidate_w"],
            "ftp_basis":         r["ftp_basis"],
            "best_20min_w":      r["best_20min"],
            "best_30min_w":      r["best_30min"],
            "best_60min_w":      r["best_60min"],
            "hr_avg":            r["hr_avg"],
            "hr_max":            r["hr_max"],
            "power_avg":         r["power_avg"],
            "power_np":          r["power_np"],
            "quality_score":     r["power_quality_score"],
            "suspicious_reason": r["suspicious_reason"],
            "source_path":       os.path.relpath(r["source_path"], PROJECT_ROOT),
        })

    fields = ["date", "sport", "sub_sport", "duration_min",
              "ftp_candidate_w", "ftp_basis",
              "best_20min_w", "best_30min_w", "best_60min_w",
              "hr_avg", "hr_max", "power_avg", "power_np",
              "quality_score", "suspicious_reason", "source_path"]
    _write_csv("suspicious_power_efforts.csv", out, fields)


# ── Report 7: top_valid_threshold_efforts ─────────────────────────────────

def top_valid_threshold_efforts(conn: sqlite3.Connection) -> None:
    """
    Best model-valid threshold efforts, ranked by FTP estimate descending.
    These are the most reliable inputs for FTP modelling and progression tracking.
    """
    rows = conn.execute("""
        SELECT
            am.start_time,
            am.sport,
            am.sub_sport,
            ROUND(am.duration_s / 60.0, 1)   AS duration_min,
            am.total_ascent_m,
            ap.ftp_candidate_w,
            ap.ftp_basis,
            ap.power_quality_score,
            ap.hr_avg,
            ap.hr_max,
            ap.power_avg,
            ap.power_np,
            pb.best_20min,
            pb.best_30min,
            pb.best_60min,
            ff.source_path
        FROM activity_performance ap
        JOIN activity_metadata am ON am.id = ap.activity_id
        JOIN power_bests pb        ON pb.activity_id = ap.activity_id
        JOIN fit_files ff          ON ff.file_hash = am.file_hash
        WHERE ap.include_in_ftp_model = 1
          AND ap.ftp_candidate_w IS NOT NULL
          AND am.start_time IS NOT NULL
          AND am.duration_s >= 1200
        ORDER BY ap.ftp_candidate_w DESC
        LIMIT 50
    """).fetchall()

    out = []
    for r in rows:
        out.append({
            "date":            (r["start_time"] or "")[:10],
            "sport":           r["sport"],
            "sub_sport":       r["sub_sport"],
            "duration_min":    r["duration_min"],
            "ftp_candidate_w": r["ftp_candidate_w"],
            "ftp_basis":       r["ftp_basis"],
            "best_20min_w":    r["best_20min"],
            "best_30min_w":    r["best_30min"],
            "best_60min_w":    r["best_60min"],
            "hr_avg":          r["hr_avg"],
            "hr_max":          r["hr_max"],
            "power_avg":       r["power_avg"],
            "normalised_power_w": r["power_np"],
            "total_ascent_m":  r["total_ascent_m"],
            "quality_score":   r["power_quality_score"],
            "source_path":     os.path.relpath(r["source_path"], PROJECT_ROOT),
        })

    fields = ["date", "sport", "sub_sport", "duration_min",
              "ftp_candidate_w", "ftp_basis",
              "best_20min_w", "best_30min_w", "best_60min_w",
              "hr_avg", "hr_max", "power_avg", "normalised_power_w",
              "total_ascent_m", "quality_score", "source_path"]
    _write_csv("top_valid_threshold_efforts.csv", out, fields)


# ── Summary to console ────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection) -> None:
    print()
    print("=== Phase 2 summary ===")

    r = conn.execute("""
        SELECT
            COUNT(*)                                          AS total,
            SUM(CASE WHEN status='done'    THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END) AS errors,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped
        FROM timeseries_imports
    """).fetchone()
    if r and r["total"]:
        print(f"  Timeseries imports : {r['done']} done / "
              f"{r['errors']} errors / {r['skipped']} skipped")

    stream_rows = conn.execute("SELECT COUNT(*) FROM activity_streams").fetchone()[0]
    print(f"  Stream records     : {stream_rows:,}")

    # ── Power validation breakdown ─────────────────────────────────────────
    print()
    print("=== Power data validation ===")

    counts = conn.execute("""
        SELECT
            power_trusted,
            is_suspicious_power,
            include_in_ftp_model,
            COUNT(*) AS cnt
        FROM activity_performance
        WHERE has_power_stream = 1
        GROUP BY power_trusted, is_suspicious_power, include_in_ftp_model
    """).fetchall()

    untrusted_n  = sum(r["cnt"] for r in counts if r["power_trusted"] == 0)
    suspicious_n = sum(r["cnt"] for r in counts
                       if r["power_trusted"] == 1 and r["is_suspicious_power"] == 1)
    valid_n      = sum(r["cnt"] for r in counts if r["include_in_ftp_model"] == 1)
    total_pwr    = sum(r["cnt"] for r in counts)

    print(f"  Total power activities     : {total_pwr}")
    print(f"  ├─ Gate 1 excluded (pre-{_POWER_TRUST_CUTOFF})  : {untrusted_n}")
    print(f"  ├─ Gate 2 excluded (suspicious) : {suspicious_n}")
    print(f"  └─ Model-valid (both gates pass): {valid_n}")

    # ── Top valid threshold efforts ────────────────────────────────────────
    print()
    print("  Top valid threshold efforts:")
    top_valid = conn.execute("""
        SELECT am.start_time, ap.ftp_candidate_w, ap.ftp_basis,
               pb.best_20min, pb.best_60min, ap.hr_avg
        FROM activity_performance ap
        JOIN activity_metadata am ON am.id = ap.activity_id
        JOIN power_bests pb        ON pb.activity_id = ap.activity_id
        WHERE ap.include_in_ftp_model = 1
          AND ap.ftp_candidate_w IS NOT NULL
        ORDER BY ap.ftp_candidate_w DESC
        LIMIT 5
    """).fetchall()
    for i, r in enumerate(top_valid, 1):
        date   = (r["start_time"] or "")[:10]
        b20    = f"20min={r['best_20min']}W" if r["best_20min"] else ""
        b60    = f"60min={r['best_60min']}W" if r["best_60min"] else ""
        hr_str = f"HR={r['hr_avg']}bpm"      if r["hr_avg"]     else ""
        print(f"    {i}. {date}  FTP~{r['ftp_candidate_w']}W ({r['ftp_basis']})"
              f"  {b20}  {b60}  {hr_str}")

    # ── Top suspicious efforts (for awareness) ─────────────────────────────
    print()
    print("  Top suspicious efforts excluded:")
    top_sus = conn.execute("""
        SELECT am.start_time, ap.ftp_candidate_w, ap.suspicious_reason,
               pb.best_20min, pb.best_60min, ap.hr_avg
        FROM activity_performance ap
        JOIN activity_metadata am ON am.id = ap.activity_id
        JOIN power_bests pb        ON pb.activity_id = ap.activity_id
        WHERE ap.power_trusted = 1
          AND ap.is_suspicious_power = 1
          AND ap.ftp_candidate_w IS NOT NULL
        ORDER BY ap.ftp_candidate_w DESC
        LIMIT 5
    """).fetchall()
    for i, r in enumerate(top_sus, 1):
        date    = (r["start_time"] or "")[:10]
        reason  = (r["suspicious_reason"] or "")[:80]
        b20     = f"20min={r['best_20min']}W" if r["best_20min"] else ""
        b60     = f"60min={r['best_60min']}W" if r["best_60min"] else ""
        hr_str  = f"HR={r['hr_avg']}bpm"      if r["hr_avg"]     else ""
        print(f"    {i}. {date}  FTP~{r['ftp_candidate_w']}W  "
              f"{b20}  {b60}  {hr_str}")
        print(f"       ↳ {reason}")

    print()


# ── Main ──────────────────────────────────────────────────────────────────

def run() -> None:
    print(f"Database : {DB_PATH}")
    print(f"Reports  : {REPORTS_DIR}")
    print()
    print("Athlete power validation rules:")
    print(f"  Gate 1 — date cutoff      : power_trusted (before {_POWER_TRUST_CUTOFF} excluded)")
    print(f"  Gate 2 — plausibility     : is_suspicious_power (ceiling/HR checks)")
    print(f"  Combined gate             : include_in_ftp_model = 1")
    print()

    conn = _open_db()

    print("Generating Phase 2 reports…")
    power_curve_by_year(conn)
    best_efforts_by_year(conn)
    aerobic_efficiency_by_year(conn)
    power_data_quality(conn)
    candidate_ftp_history(conn)
    suspicious_power_efforts(conn)
    top_valid_threshold_efforts(conn)

    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    run()
