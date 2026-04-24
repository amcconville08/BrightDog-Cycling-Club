"""
generate_phase2_reports.py — Phase 2 CSV reports from the performance database.

Usage:
    python importer/generate_phase2_reports.py

Output (data/reports/):
    power_curve_by_year.csv        Best MMP per duration per year
    best_efforts_by_year.csv       Top-3 efforts per duration per year
    aerobic_efficiency_by_year.csv Efficiency Factor trend by year
    power_data_quality.csv         Quality score per power activity
    candidate_ftp_history.csv      FTP estimates in date order
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
             "best_5min", "best_10min", "best_20min", "best_60min"]


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
    Only activities where power_quality_score >= 50 are counted.
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
          AND ap.power_quality_score >= 50
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
    Useful for spotting personal bests and seasonal peaks.
    """
    out = []
    for col in _MMP_COLS:
        dur_label = col.replace("best_", "")
        # Get all activities with this duration's best, ranked within year
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
              AND ap.power_quality_score >= 50
            ORDER BY year, pb.{col} DESC
        """).fetchall()

        # Take top 3 per year
        seen: dict = {}
        for r in rows:
            yr = r["year"]
            if yr not in seen:
                seen[yr] = 0
            if seen[yr] < 3:
                out.append({
                    "duration":    dur_label,
                    "year":        yr,
                    "rank":        seen[yr] + 1,
                    "date":        (r["start_time"] or "")[:10],
                    "watts":       r["watts"],
                    "quality_pct": r["quality"],
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
    Improving EF over time = improving aerobic fitness.
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
    Per-activity power data quality report.
    Helps identify rides suitable for FTP estimation vs noisy/partial data.
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
              "ftp_candidate_w", "ftp_basis", "source_path"]
    _write_csv("power_data_quality.csv", out, fields)


# ── Report 5: candidate_ftp_history ──────────────────────────────────────

def candidate_ftp_history(conn: sqlite3.Connection) -> None:
    """
    Chronological FTP estimates from all activities where one can be computed.
    Higher quality_score = more reliable estimate.

    Methodology:
      - If best_60min exists → FTP candidate = best_60min W
      - If best_20min exists → FTP candidate = best_20min × 0.95
    Only activities with quality_score >= 60 are included.
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
            pb.best_60min
        FROM activity_performance ap
        JOIN activity_metadata am  ON am.id = ap.activity_id
        JOIN power_bests pb         ON pb.activity_id = ap.activity_id
        WHERE ap.ftp_candidate_w IS NOT NULL
          AND ap.power_quality_score >= 60
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
            "best_60min_w":        r["best_60min"],
            "normalised_power_w":  r["power_np"],
            "quality_score":       r["power_quality_score"],
        })

    fields = ["date", "sport", "duration_min", "ftp_candidate_w", "ftp_basis",
              "best_20min_w", "best_60min_w", "normalised_power_w", "quality_score"]
    _write_csv("candidate_ftp_history.csv", out, fields)


# ── Summary to console ────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection) -> None:
    print()
    print("=== Phase 2 summary ===")

    # Activities imported
    r = conn.execute("""
        SELECT
            COUNT(*)                                     AS total,
            SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status='error'  THEN 1 ELSE 0 END) AS errors,
            SUM(CASE WHEN status='skipped'THEN 1 ELSE 0 END) AS skipped
        FROM timeseries_imports
    """).fetchone()
    if r and r["total"]:
        print(f"  Timeseries imports : {r['done']} done / {r['errors']} errors / {r['skipped']} skipped")

    stream_rows = conn.execute("SELECT COUNT(*) FROM activity_streams").fetchone()[0]
    print(f"  Stream records     : {stream_rows:,}")

    # FTP history
    ftp_rows = conn.execute("""
        SELECT COUNT(*) FROM activity_performance
        WHERE ftp_candidate_w IS NOT NULL AND power_quality_score >= 60
    """).fetchone()[0]
    print(f"  FTP candidates     : {ftp_rows}")

    # Best ever
    best = conn.execute("""
        SELECT am.start_time, pb.best_20min, pb.best_60min
        FROM power_bests pb
        JOIN activity_metadata am ON am.id = pb.activity_id
        ORDER BY COALESCE(pb.best_60min, pb.best_20min * 0.95) DESC NULLS LAST
        LIMIT 1
    """).fetchone()
    if best:
        b60  = f"60-min best: {best['best_60min']}W"  if best["best_60min"] else ""
        b20  = f"20-min best: {best['best_20min']}W"  if best["best_20min"] else ""
        date = (best["start_time"] or "")[:10]
        print(f"  Highest effort     : {date}  {b60 or b20}")

    print()


# ── Main ──────────────────────────────────────────────────────────────────

def run() -> None:
    print(f"Database : {DB_PATH}")
    print(f"Reports  : {REPORTS_DIR}")
    print()

    conn = _open_db()

    print("Generating Phase 2 reports…")
    power_curve_by_year(conn)
    best_efforts_by_year(conn)
    aerobic_efficiency_by_year(conn)
    power_data_quality(conn)
    candidate_ftp_history(conn)

    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    run()
