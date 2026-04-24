"""
import_metadata.py — Main FIT file importer.

Usage:
    python importer/import_metadata.py [--verbose] [--limit N]

Behaviour:
    - Scans garmin_export/ and garmin_device/ for .fit files
    - Computes SHA-256 hash for each file
    - Skips files already imported (hash already in fit_files + activity_metadata)
    - Updates source_path if a known file has moved
    - Logs unreadable files to import_errors table
    - Safe to run repeatedly (idempotent)
"""
import argparse
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
_THIS_DIR    = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent
DB_PATH      = PROJECT_ROOT / "data" / "processed" / "sqlite" / "garmin_history.db"
SCHEMA_PATH  = _THIS_DIR / "schema.sql"

sys.path.insert(0, str(_THIS_DIR))  # so scan_fit_files / fit_parser are importable
import scan_fit_files
import fit_parser


# ── Database helpers ──────────────────────────────────────────────────────

def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    sql = schema_path.read_text()
    conn.executescript(sql)
    conn.commit()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Import helpers ────────────────────────────────────────────────────────

def _get_fit_file_row(conn: sqlite3.Connection, file_hash: str):
    return conn.execute(
        "SELECT * FROM fit_files WHERE file_hash = ?", (file_hash,)
    ).fetchone()


def _is_activity_imported(conn: sqlite3.Connection, file_hash: str) -> bool:
    """True if this hash already has a row in activity_metadata or import_errors."""
    has_meta = conn.execute(
        "SELECT 1 FROM activity_metadata WHERE file_hash = ?", (file_hash,)
    ).fetchone()
    if has_meta:
        return True
    has_err = conn.execute(
        "SELECT 1 FROM import_errors WHERE file_hash = ?", (file_hash,)
    ).fetchone()
    return has_err is not None


def _upsert_fit_file(conn: sqlite3.Connection, info: dict, source: str) -> int:
    """
    Insert a new fit_files row, or update source_path / last_seen if it moved.
    Returns the row id.
    """
    now = _now_utc()
    existing = _get_fit_file_row(conn, info["file_hash"])

    if existing is None:
        cur = conn.execute(
            """INSERT INTO fit_files
               (file_hash, file_name, source_path, source, file_size_b, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                info["file_hash"], info["file_name"], info["source_path"],
                source, info["file_size_b"], now, now,
            ),
        )
        return cur.lastrowid
    else:
        # Update path and last_seen if file has moved
        if existing["source_path"] != info["source_path"]:
            conn.execute(
                "UPDATE fit_files SET source_path = ?, last_seen = ? WHERE id = ?",
                (info["source_path"], now, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE fit_files SET last_seen = ? WHERE id = ?",
                (now, existing["id"]),
            )
        return existing["id"]


def _insert_activity(conn: sqlite3.Connection, fit_file_id: int, file_hash: str, meta: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO activity_metadata
           (fit_file_id, file_hash, start_time, sport, sub_sport,
            duration_s, distance_m, total_ascent_m,
            avg_heart_rate, max_heart_rate,
            avg_power, max_power, avg_cadence,
            has_hr, has_power, has_gps, parsed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fit_file_id, file_hash,
            meta.get("start_time"),
            meta.get("sport"),
            meta.get("sub_sport"),
            meta.get("duration_s"),
            meta.get("distance_m"),
            meta.get("total_ascent_m"),
            meta.get("avg_heart_rate"),
            meta.get("max_heart_rate"),
            meta.get("avg_power"),
            meta.get("max_power"),
            meta.get("avg_cadence"),
            1 if meta.get("has_hr")    else 0,
            1 if meta.get("has_power") else 0,
            1 if meta.get("has_gps")   else 0,
            _now_utc(),
        ),
    )


def _insert_error(
    conn: sqlite3.Connection,
    fit_file_id: int,
    file_hash: str,
    source_path: str,
    exc: Exception,
) -> None:
    conn.execute(
        """INSERT INTO import_errors
           (fit_file_id, file_hash, source_path, error_type, error_message, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            fit_file_id,
            file_hash,
            source_path,
            type(exc).__name__,
            str(exc)[:1000],  # cap at 1000 chars
            _now_utc(),
        ),
    )


# ── Main ──────────────────────────────────────────────────────────────────

def run(verbose: bool = False, limit: int = 0) -> None:
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Database     : {DB_PATH}")
    print()

    conn = _open_db(DB_PATH)
    _init_schema(conn, SCHEMA_PATH)

    # ── Scan for FIT files ────────────────────────────────────────────────
    print("Scanning for FIT files…")
    all_files = scan_fit_files.scan(str(PROJECT_ROOT), verbose=verbose)
    print(f"Found {len(all_files)} FIT files total\n")

    if limit:
        all_files = all_files[:limit]
        print(f"(Limited to first {limit} files for testing)\n")

    # ── Process each file ─────────────────────────────────────────────────
    stats = {"new": 0, "skipped": 0, "path_updated": 0, "errors": 0}
    total = len(all_files)

    for i, file_info in enumerate(all_files, 1):
        path       = file_info["source_path"]
        source     = file_info["source"]
        short_path = os.path.relpath(path, PROJECT_ROOT)

        # Progress line
        if verbose or (i % 100 == 0) or i == total:
            print(f"  [{i:>4}/{total}] {short_path}")

        # ── Hash ──────────────────────────────────────────────────────────
        try:
            file_info["file_hash"] = fit_parser.sha256_file(path)
            file_info["file_name"] = os.path.basename(path)
        except OSError as exc:
            print(f"  [ERROR] Cannot read {short_path}: {exc}", file=sys.stderr)
            stats["errors"] += 1
            continue

        # ── Upsert fit_files row ──────────────────────────────────────────
        existing_before = _get_fit_file_row(conn, file_info["file_hash"])
        fit_file_id = _upsert_fit_file(conn, file_info, source)

        # Track path changes
        if existing_before and existing_before["source_path"] != path:
            if verbose:
                print(f"  [PATH UPDATED] {short_path}")
            stats["path_updated"] += 1

        # ── Skip if already fully imported ────────────────────────────────
        if _is_activity_imported(conn, file_info["file_hash"]):
            stats["skipped"] += 1
            conn.commit()
            continue

        # ── Parse FIT metadata ────────────────────────────────────────────
        try:
            meta = fit_parser.parse_fit(path)
            _insert_activity(conn, fit_file_id, file_info["file_hash"], meta)
            stats["new"] += 1
            if verbose:
                sport    = meta.get("sport") or "?"
                start    = (meta.get("start_time") or "?")[:10]
                dur_min  = int((meta.get("duration_s") or 0) / 60)
                flags    = "".join([
                    "H" if meta.get("has_hr")    else "-",
                    "P" if meta.get("has_power") else "-",
                    "G" if meta.get("has_gps")   else "-",
                ])
                print(f"    → {sport:16} {start}  {dur_min:>4}min  [{flags}]")

        except Exception as exc:
            err_short = str(exc)[:120]
            print(f"  [PARSE ERROR] {short_path}: {err_short}", file=sys.stderr)
            _insert_error(conn, fit_file_id, file_info["file_hash"], path, exc)
            stats["errors"] += 1

        conn.commit()

    # ── Summary ───────────────────────────────────────────────────────────
    print()
    print("─" * 50)
    print(f"  Imported (new)   : {stats['new']}")
    print(f"  Skipped (cached) : {stats['skipped']}")
    print(f"  Path updated     : {stats['path_updated']}")
    print(f"  Errors           : {stats['errors']}")
    print("─" * 50)

    # Final counts from DB
    total_meta   = conn.execute("SELECT COUNT(*) FROM activity_metadata").fetchone()[0]
    total_errors = conn.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0]
    print(f"  DB total — parsed: {total_meta}  errors: {total_errors}")
    print()

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Garmin FIT file metadata")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-file detail")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="Process only N files (for testing)")
    args = parser.parse_args()
    run(verbose=args.verbose, limit=args.limit)
