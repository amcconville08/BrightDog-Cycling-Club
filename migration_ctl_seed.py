#!/usr/bin/env python3
"""
migration_ctl_seed.py — Compute historical CTL/ATL from Garmin history and
write a ctl_seed record into club.db so the poller can start from a realistic baseline.

Run ON THE SERVER (not inside Docker) — needs access to garmin_history.db.

Usage:
    python3 migration_ctl_seed.py [--garmin-db PATH] [--club-db PATH] [--user-id ID]
"""
import sys
import sqlite3
import math
import json
import argparse
from datetime import datetime, date, timedelta, timezone

# ── Constants ─────────────────────────────────────────────────────────────────

_GARMIN_DB = "/mnt/media2tb/aidan-ai-projects/brightdog-cycling-club/data/processed/sqlite/garmin_history.db"
_CLUB_DB   = "/opt/cycling-club/data/club.db"
_USER_ID   = 1

_CTL_DECAY = math.exp(-1 / 42)
_ATL_DECAY = math.exp(-1 / 7)

# Stepped FTP lookup: (date_str, ftp_watts)
# Active FTP from each date onward until the next entry.
# Built from verified historical peaks + known life events.
_FTP_STEPS = [
    ("2012-01-01", 180),   # Pre-Garmin era; default estimate
    ("2016-12-20", 200),   # Garmin history begins
    ("2018-01-01", 215),   # Gradual improvement
    ("2018-06-25", 235),   # Accurate power meter installed; building from verified baseline
    ("2019-01-01", 255),   # Approaching form
    ("2019-03-01", 268),   # Strong spring build
    ("2019-05-07", 280),   # Post-peak; FTP at 280W (2019-05-06 peak date + 1)
    ("2019-06-08", 200),   # Crash — broken pelvis + sacrum. Training halted.
    ("2019-09-01", 210),   # Return to easy riding
    ("2019-11-01", 225),   # Gradual rebuild
    ("2020-01-01", 240),   # Building through winter
    ("2020-04-01", 260),   # Spring form returning
    ("2020-07-21", 279),   # Second peak (2020-07-20); FTP reflects achieved level
    ("2020-10-01", 265),   # Post-peak summer
    ("2021-01-01", 245),   # Winter dip
    ("2021-04-01", 258),   # Spring build
    ("2021-10-01", 250),   # Autumn
    ("2022-01-01", 238),   # Winter
    ("2022-05-01", 255),   # Building
    ("2022-10-01", 248),   # Autumn
    ("2023-01-01", 255),   # Building toward March peak
    ("2023-03-25", 275),   # Third peak (2023-03-24); post-peak FTP
    ("2023-07-01", 265),   # Summer maintenance
    ("2023-10-01", 255),   # Autumn
    ("2024-01-01", 248),   # 2024 base
    ("2024-06-01", 255),   # Summer form
    ("2024-09-01", 235),   # Brain tumour diagnosis; training impact
    ("2025-01-01", 215),   # Treatment / reduced training
    ("2025-04-01", 225),   # Gradual return
    ("2025-07-01", 235),   # Rebuilding
    ("2025-10-01", 238),   # Autumn base
    ("2026-01-01", 240),   # Comeback building
    ("2026-04-01", 250),   # FTP updated in app settings
]


def _ftp_at(date_str: str) -> float:
    """Return interpolated FTP for a given date string (YYYY-MM-DD)."""
    ftp = _FTP_STEPS[0][1]
    for step_date, step_ftp in _FTP_STEPS:
        if date_str >= step_date:
            ftp = step_ftp
        else:
            break
    return float(ftp)


def _tss(np_watts: float, duration_s: float, ftp: float) -> float:
    """Compute TSS from NP, duration, and FTP."""
    if ftp <= 0 or duration_s <= 0 or np_watts <= 0:
        return 0.0
    return (duration_s * (np_watts / ftp) ** 2 * 100.0) / 3600.0


def _estimated_tss(duration_s: float, ftp: float, if_factor: float = 0.68) -> float:
    """
    Estimate TSS for rides with no reliable power data.
    Uses typical endurance IF of 0.68 — gives TSS ≈ 46 per hour.
    """
    if duration_s <= 0:
        return 0.0
    return (duration_s * (if_factor * ftp) ** 2 * 100.0) / (3600.0 * ftp ** 2)
    # Simplifies to: (duration_s / 3600) * if_factor^2 * 100


def load_garmin_activities(garmin_db: str) -> list:
    """
    Load all cycling activities from garmin_history.db.
    Returns list of dicts sorted by start_date ascending.
    """
    conn = sqlite3.connect(garmin_db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            date(m.start_time) AS start_date,
            m.duration_s,
            p.power_np,
            p.power_avg,
            p.power_trusted
        FROM activity_metadata m
        LEFT JOIN activity_performance p ON p.activity_id = m.id
        WHERE m.sport = 'cycling'
          AND m.duration_s > 0
        ORDER BY m.start_time ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_daily_tss(activities: list) -> dict:
    """
    Aggregate activities into a daily TSS map {date_str: tss}.
    Uses power_np when trusted, falls back to duration estimate.
    """
    daily = {}
    for a in activities:
        date_str = a["start_date"]
        if not date_str:
            continue

        ftp = _ftp_at(date_str)
        np  = a["power_np"]
        trusted = a["power_trusted"]
        dur_s = float(a["duration_s"] or 0)

        if np and trusted and float(np) > 80:
            tss = _tss(float(np), dur_s, ftp)
        elif a["power_avg"] and trusted and float(a["power_avg"]) > 80:
            # NP missing but avg watts present — use avg as rough proxy (underestimates slightly)
            tss = _tss(float(a["power_avg"]) * 1.05, dur_s, ftp)  # small VI correction
        else:
            # No reliable power — estimate from duration
            tss = _estimated_tss(dur_s, ftp, if_factor=0.68)

        daily[date_str] = daily.get(date_str, 0.0) + tss

    return daily


def run_ewma(daily_tss: dict, up_to: date) -> tuple:
    """
    Run EWMA over daily TSS map from first known date through up_to.
    Returns (ctl, atl, prev_ctl) where prev_ctl is CTL 7 days before up_to.
    """
    if not daily_tss:
        return 0.0, 0.0, 0.0

    first = min(date.fromisoformat(d) for d in daily_tss)
    current = first
    ctl = 0.0
    atl = 0.0
    prev_ctl = 0.0
    seven_ago = up_to - timedelta(days=7)

    while current <= up_to:
        tss = daily_tss.get(current.isoformat(), 0.0)
        ctl = ctl * _CTL_DECAY + tss * (1 - _CTL_DECAY)
        atl = atl * _ATL_DECAY + tss * (1 - _ATL_DECAY)
        if current == seven_ago:
            prev_ctl = ctl
        current += timedelta(days=1)

    return ctl, atl, prev_ctl


def ensure_ctl_seed_table(club_db: str):
    """Create the ctl_seed table if it doesn't exist."""
    conn = sqlite3.connect(club_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ctl_seed (
            user_id      INTEGER PRIMARY KEY,
            ctl          REAL    NOT NULL DEFAULT 0,
            atl          REAL    NOT NULL DEFAULT 0,
            prev_ctl     REAL    NOT NULL DEFAULT 0,
            seed_date    TEXT    NOT NULL,
            daily_tss_json TEXT,
            computed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            source       TEXT    NOT NULL DEFAULT 'garmin'
        )
    """)
    conn.commit()
    conn.close()


def save_ctl_seed(club_db: str, user_id: int, ctl: float, atl: float,
                  prev_ctl: float, seed_date: date, daily_tss: dict):
    """Write or replace the CTL seed for this user."""
    conn = sqlite3.connect(club_db)
    conn.execute("""
        INSERT INTO ctl_seed (user_id, ctl, atl, prev_ctl, seed_date, daily_tss_json, source)
        VALUES (?, ?, ?, ?, ?, ?, 'garmin')
        ON CONFLICT(user_id) DO UPDATE SET
            ctl=excluded.ctl, atl=excluded.atl, prev_ctl=excluded.prev_ctl,
            seed_date=excluded.seed_date, daily_tss_json=excluded.daily_tss_json,
            computed_at=datetime('now'), source='garmin'
    """, (user_id, round(ctl, 2), round(atl, 2), round(prev_ctl, 2),
          seed_date.isoformat(), json.dumps(daily_tss)))
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Seed CTL from Garmin history")
    parser.add_argument("--garmin-db", default=_GARMIN_DB)
    parser.add_argument("--club-db",   default=_CLUB_DB)
    parser.add_argument("--user-id",   type=int, default=_USER_ID)
    args = parser.parse_args()

    today = date.today()
    print(f"Seeding CTL for user {args.user_id} from Garmin history")
    print(f"  garmin_db : {args.garmin_db}")
    print(f"  club_db   : {args.club_db}")
    print(f"  up_to     : {today}")

    print("\nLoading Garmin activities…")
    activities = load_garmin_activities(args.garmin_db)
    print(f"  Loaded {len(activities)} cycling activities")

    dates = [a["start_date"] for a in activities if a["start_date"]]
    if dates:
        print(f"  Date range: {min(dates)} → {max(dates)}")

    print("\nComputing daily TSS…")
    daily_tss = compute_daily_tss(activities)
    total_tss = sum(daily_tss.values())
    days_with_tss = len([v for v in daily_tss.values() if v > 0])
    print(f"  {days_with_tss} days with TSS > 0")
    print(f"  Total historical TSS: {total_tss:.0f}")
    print(f"  Avg TSS on training days: {total_tss/max(days_with_tss,1):.1f}")

    print("\nRunning EWMA…")
    ctl, atl, prev_ctl = run_ewma(daily_tss, today)
    tsb = ctl - atl
    print(f"  CTL = {ctl:.1f}")
    print(f"  ATL = {atl:.1f}")
    print(f"  TSB = {tsb:.1f}")
    print(f"  Prev CTL (7d ago) = {prev_ctl:.1f}")

    print("\nSaving to club.db…")
    ensure_ctl_seed_table(args.club_db)
    save_ctl_seed(args.club_db, args.user_id, ctl, atl, prev_ctl, today, daily_tss)
    print("  Done.")

    print(f"\n✓ CTL seed written: CTL={ctl:.1f} ATL={atl:.1f} TSB={tsb:.1f}")
    print("  Run the poller or restart the Docker container to apply.")


if __name__ == "__main__":
    main()
