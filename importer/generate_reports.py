"""
generate_reports.py — Query garmin_history.db and write CSV reports.

Usage:
    python importer/generate_reports.py

Output files (in data/reports/):
    coverage_by_year.csv       — activity counts, HR/power/GPS coverage per year
    data_quality_summary.csv   — overall stats and data quality metrics
    power_data_by_year.csv     — power-specific stats per year
    import_errors.csv          — files that failed to parse
"""
import csv
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

_THIS_DIR    = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent
DB_PATH      = PROJECT_ROOT / "data" / "processed" / "sqlite" / "garmin_history.db"
REPORTS_DIR  = PROJECT_ROOT / "data" / "reports"


# ── Helpers ───────────────────────────────────────────────────────────────

def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        print("Run: python importer/import_metadata.py", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _write_csv(path: Path, rows: list, fieldnames: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Written: {os.path.relpath(path, PROJECT_ROOT)}")


def _year(iso_str: str | None) -> str | None:
    """Extract 4-digit year from an ISO-8601 string."""
    if not iso_str:
        return None
    try:
        return iso_str[:4]
    except Exception:
        return None


CYCLING_SPORTS = ("cycling",)  # fitparse sport values that map to cycling


def _is_cycling(sport: str | None) -> bool:
    """True if the sport field looks like a cycling activity."""
    if not sport:
        return False
    return "cycling" in sport.lower()


# ── Report generators ─────────────────────────────────────────────────────

def coverage_by_year(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    coverage_by_year.csv
    Columns: year, total_activities, cycling_activities,
             with_hr, with_power, with_gps,
             pct_hr, pct_power, pct_gps
    """
    rows = conn.execute("""
        SELECT
            SUBSTR(start_time, 1, 4)                        AS year,
            COUNT(*)                                         AS total_activities,
            SUM(CASE WHEN sport LIKE '%cycling%' THEN 1 ELSE 0 END)
                                                             AS cycling_activities,
            SUM(has_hr)                                      AS with_hr,
            SUM(has_power)                                   AS with_power,
            SUM(has_gps)                                     AS with_gps
        FROM activity_metadata
        WHERE start_time IS NOT NULL
        GROUP BY year
        ORDER BY year
    """).fetchall()

    out = []
    for r in rows:
        total = r["total_activities"] or 1
        out.append({
            "year":                r["year"],
            "total_activities":    r["total_activities"],
            "cycling_activities":  r["cycling_activities"],
            "with_hr":             r["with_hr"],
            "with_power":          r["with_power"],
            "with_gps":            r["with_gps"],
            "pct_hr":              f"{r['with_hr']  / total * 100:.0f}%",
            "pct_power":           f"{r['with_power']/ total * 100:.0f}%",
            "pct_gps":             f"{r['with_gps']  / total * 100:.0f}%",
        })

    fields = ["year", "total_activities", "cycling_activities",
              "with_hr", "with_power", "with_gps",
              "pct_hr", "pct_power", "pct_gps"]
    _write_csv(out_dir / "coverage_by_year.csv", out, fields)


def data_quality_summary(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    data_quality_summary.csv
    Flat key/value table of overall quality metrics.
    """
    c = conn

    total_fit_files   = c.execute("SELECT COUNT(*) FROM fit_files").fetchone()[0]
    total_parsed      = c.execute("SELECT COUNT(*) FROM activity_metadata").fetchone()[0]
    total_errors      = c.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0]
    with_hr           = c.execute("SELECT COUNT(*) FROM activity_metadata WHERE has_hr   = 1").fetchone()[0]
    with_power        = c.execute("SELECT COUNT(*) FROM activity_metadata WHERE has_power = 1").fetchone()[0]
    with_gps          = c.execute("SELECT COUNT(*) FROM activity_metadata WHERE has_gps  = 1").fetchone()[0]
    cycling           = c.execute(
        "SELECT COUNT(*) FROM activity_metadata WHERE sport LIKE '%cycling%'"
    ).fetchone()[0]

    first_activity = c.execute(
        "SELECT MIN(start_time) FROM activity_metadata WHERE start_time IS NOT NULL"
    ).fetchone()[0]
    last_activity = c.execute(
        "SELECT MAX(start_time) FROM activity_metadata WHERE start_time IS NOT NULL"
    ).fetchone()[0]

    first_power_year = c.execute(
        "SELECT MIN(SUBSTR(start_time, 1, 4)) FROM activity_metadata "
        "WHERE has_power = 1 AND start_time IS NOT NULL"
    ).fetchone()[0]

    no_start_time = c.execute(
        "SELECT COUNT(*) FROM activity_metadata WHERE start_time IS NULL"
    ).fetchone()[0]

    rows = [
        {"metric": "total_fit_files_found",     "value": total_fit_files},
        {"metric": "successfully_parsed",        "value": total_parsed},
        {"metric": "parse_errors",               "value": total_errors},
        {"metric": "parse_success_rate",         "value": f"{total_parsed / max(total_fit_files, 1) * 100:.1f}%"},
        {"metric": "no_start_time",              "value": no_start_time},
        {"metric": "activities_with_hr",         "value": with_hr},
        {"metric": "activities_with_power",      "value": with_power},
        {"metric": "activities_with_gps",        "value": with_gps},
        {"metric": "likely_cycling_activities",  "value": cycling},
        {"metric": "first_activity_date",        "value": (first_activity or "")[:10]},
        {"metric": "last_activity_date",         "value": (last_activity  or "")[:10]},
        {"metric": "first_year_with_power",      "value": first_power_year or "N/A"},
        {"metric": "report_generated_at",        "value": datetime.now(timezone.utc).isoformat()},
    ]
    _write_csv(out_dir / "data_quality_summary.csv", rows, ["metric", "value"])


def power_data_by_year(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    power_data_by_year.csv
    Columns: year, activities_with_power, avg_power_mean, max_power_peak
    """
    rows = conn.execute("""
        SELECT
            SUBSTR(start_time, 1, 4)    AS year,
            COUNT(*)                    AS activities_with_power,
            ROUND(AVG(avg_power), 0)    AS avg_power_mean,
            MAX(max_power)              AS max_power_peak
        FROM activity_metadata
        WHERE has_power = 1
          AND start_time IS NOT NULL
        GROUP BY year
        ORDER BY year
    """).fetchall()

    out = [
        {
            "year":                 r["year"],
            "activities_with_power": r["activities_with_power"],
            "avg_power_mean_w":     r["avg_power_mean"],
            "max_power_peak_w":     r["max_power_peak"],
        }
        for r in rows
    ]

    fields = ["year", "activities_with_power", "avg_power_mean_w", "max_power_peak_w"]
    _write_csv(out_dir / "power_data_by_year.csv", out, fields)


def import_errors_report(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    import_errors.csv
    All files that failed to parse, with error details.
    """
    rows = conn.execute("""
        SELECT
            ie.source_path,
            ff.source,
            ff.file_size_b,
            ie.error_type,
            ie.error_message,
            ie.occurred_at
        FROM import_errors ie
        LEFT JOIN fit_files ff ON ff.id = ie.fit_file_id
        ORDER BY ie.occurred_at
    """).fetchall()

    out = [
        {
            "source_path":   r["source_path"],
            "source":        r["source"],
            "file_size_b":   r["file_size_b"],
            "error_type":    r["error_type"],
            "error_message": r["error_message"],
            "occurred_at":   r["occurred_at"],
        }
        for r in rows
    ]

    fields = ["source_path", "source", "file_size_b",
              "error_type", "error_message", "occurred_at"]
    _write_csv(out_dir / "import_errors.csv", out, fields)


# ── Main ──────────────────────────────────────────────────────────────────

def run() -> None:
    print(f"Database : {DB_PATH}")
    print(f"Reports  : {REPORTS_DIR}")
    print()

    conn = _open_db(DB_PATH)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("Generating reports…")
    coverage_by_year(conn, REPORTS_DIR)
    data_quality_summary(conn, REPORTS_DIR)
    power_data_by_year(conn, REPORTS_DIR)
    import_errors_report(conn, REPORTS_DIR)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
