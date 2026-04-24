"""
metrics.py - Pure fitness metric computations.
No I/O, no side effects. All functions take plain Python data and return plain Python data.
EWMA-based CTL/ATL/TSB following the Banister impulse-response model.
"""
import math
from datetime import datetime, timezone, timedelta, date as date_cls

_CTL_DECAY = math.exp(-1 / 42)
_ATL_DECAY = math.exp(-1 / 7)

CYCLING_TYPES = {
    "Ride",
    "GravelRide",
    "MountainBikeRide",
    "VirtualRide",
    "EBikeRide",
}


def compute_tss(np_watts: float, duration_s: float, ftp: float) -> float:
    """Compute Training Stress Score from normalised power, duration, and FTP."""
    if ftp <= 0 or duration_s <= 0:
        return 0.0
    return (duration_s * (np_watts / ftp) ** 2 * 100) / 3600


def estimate_tss(activity: dict, ftp: float) -> float:
    """
    Estimate TSS for a single Strava activity dict.
    Priority:
      1. weighted_average_watts present → TSS formula
      2. suffer_score present → suffer_score * 0.5
      3. fallback → 0
    """
    if ftp <= 0:
        return 0.0

    np_watts = activity.get("weighted_average_watts") or activity.get("average_watts")
    duration_s = activity.get("moving_time") or activity.get("elapsed_time") or 0

    if np_watts and np_watts > 0 and duration_s > 0:
        return compute_tss(float(np_watts), float(duration_s), ftp)

    suffer = activity.get("suffer_score")
    if suffer is not None and suffer > 0:
        return float(suffer) * 0.5

    return 0.0


def _parse_start_ts(activity: dict) -> float:
    """Return activity start as a UTC unix timestamp float."""
    raw = activity.get("start_date") or activity.get("start_date_local") or ""
    if not raw:
        return 0.0
    try:
        dt = datetime.strptime(raw.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def compute_metrics(activities: list, ftp: float, ctl_seed: dict = None) -> dict:
    """
    Compute all fitness metrics from a list of raw Strava activity dicts.

    If ctl_seed is provided (dict with keys 'ctl', 'atl', 'prev_ctl', 'seed_date'),
    the EWMA starts from the seed values at seed_date and only processes Strava
    activities AFTER seed_date (to avoid double-counting with Garmin history).

    Returns a dict with keys prefixed 'cycling_' matching the exporter schema.
    Only activities whose type is in CYCLING_TYPES are included.
    """
    now_utc = datetime.now(timezone.utc)
    today_date = now_utc.date()

    # Filter to cycling activities only, attach parsed timestamps
    rides = []
    for act in activities:
        if act.get("type") not in CYCLING_TYPES and act.get("sport_type") not in CYCLING_TYPES:
            continue
        ts = _parse_start_ts(act)
        if ts <= 0:
            continue
        rides.append((ts, act))

    # Sort oldest first for EWMA traversal
    rides.sort(key=lambda x: x[0])

    # ---------------------------------------------------------------------------
    # Determine EWMA starting point
    # ---------------------------------------------------------------------------
    use_seed = False
    seed_date = None
    ctl = 0.0
    atl = 0.0
    prev_ctl_seed = 0.0

    if ctl_seed and ctl_seed.get("seed_date") and float(ctl_seed.get("ctl", 0)) > 0:
        try:
            seed_date = date_cls.fromisoformat(str(ctl_seed["seed_date"]))
            ctl = float(ctl_seed.get("ctl", 0))
            atl = float(ctl_seed.get("atl", 0))
            prev_ctl_seed = float(ctl_seed.get("prev_ctl", 0))
            use_seed = True
        except Exception:
            use_seed = False

    # ---------------------------------------------------------------------------
    # Build a daily TSS series for EWMA — only include rides in scope
    # ---------------------------------------------------------------------------
    daily_tss_map: dict = {}
    for ts, act in rides:
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if use_seed and dt_utc <= seed_date:
            # Garmin seed covers this period — skip to avoid double-counting
            continue
        tss = estimate_tss(act, ftp)
        daily_tss_map[dt_utc] = daily_tss_map.get(dt_utc, 0.0) + tss

    # ---------------------------------------------------------------------------
    # EWMA for CTL and ATL
    # ---------------------------------------------------------------------------
    prev_ctl = prev_ctl_seed  # default to seed's prev_ctl value
    seven_days_ago = today_date - timedelta(days=7)

    if use_seed:
        # Start EWMA from day after seed_date, using seeded CTL/ATL as initial values
        ewma_start = seed_date + timedelta(days=1)
    elif daily_tss_map:
        ewma_start = min(daily_tss_map.keys())
        ctl = 0.0
        atl = 0.0
    else:
        ewma_start = today_date

    current = ewma_start
    while current <= today_date:
        tss_today = daily_tss_map.get(current, 0.0)
        ctl = ctl * _CTL_DECAY + tss_today * (1 - _CTL_DECAY)
        atl = atl * _ATL_DECAY + tss_today * (1 - _ATL_DECAY)
        if current == seven_days_ago:
            prev_ctl = ctl
        current += timedelta(days=1)

    tsb = ctl - atl

    # ---------------------------------------------------------------------------
    # Today's TSS and kJ (from all rides, regardless of seed)
    # ---------------------------------------------------------------------------
    daily_tss = 0.0
    daily_kj = 0.0
    for ts, act in rides:
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if dt_utc == today_date:
            daily_tss += estimate_tss(act, ftp)
            kj = act.get("kilojoules") or 0.0
            daily_kj += float(kj)

    # ---------------------------------------------------------------------------
    # Last activity (most recent ride, regardless of date or seed)
    # ---------------------------------------------------------------------------
    last_distance_m = 0.0
    last_moving_time_s = 0.0
    last_elapsed_time_s = 0.0
    last_elevation_m = 0.0
    last_avg_watts = 0.0
    last_start_ts = 0.0

    if rides:
        last_ts, last_act = rides[-1]
        last_distance_m = float(last_act.get("distance") or 0)
        last_moving_time_s = float(last_act.get("moving_time") or 0)
        last_elapsed_time_s = float(last_act.get("elapsed_time") or 0)
        last_elevation_m = float(last_act.get("total_elevation_gain") or 0)
        last_avg_watts = float(last_act.get("average_watts") or 0)
        last_start_ts = last_ts

    # ---------------------------------------------------------------------------
    # Weekly totals (Mon–Sun of current week, UTC)
    # ---------------------------------------------------------------------------
    week_start = today_date - timedelta(days=today_date.weekday())
    week_end = week_start + timedelta(days=6)

    weekly_distance_m = 0.0
    weekly_elevation_m = 0.0
    weekly_count = 0
    weekly_moving_time_s = 0.0
    weekly_tss = 0.0
    weekly_longest_distance_m = 0.0

    for ts, act in rides:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if week_start <= dt <= week_end:
            dist = float(act.get("distance") or 0)
            weekly_distance_m += dist
            weekly_elevation_m += float(act.get("total_elevation_gain") or 0)
            weekly_moving_time_s += float(act.get("moving_time") or 0)
            weekly_tss += estimate_tss(act, ftp)
            weekly_count += 1
            if dist > weekly_longest_distance_m:
                weekly_longest_distance_m = dist

    # ---------------------------------------------------------------------------
    # Monthly totals (current calendar month, UTC)
    # ---------------------------------------------------------------------------
    month_start = today_date.replace(day=1)

    monthly_distance_m = 0.0
    monthly_elevation_m = 0.0
    monthly_count = 0

    for ts, act in rides:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if dt >= month_start:
            monthly_distance_m += float(act.get("distance") or 0)
            monthly_elevation_m += float(act.get("total_elevation_gain") or 0)
            monthly_count += 1

    # ---------------------------------------------------------------------------
    # Rolling 7-day window (last 7 full days, not week-aligned)
    # ---------------------------------------------------------------------------
    rolling_cutoff = today_date - timedelta(days=7)

    rolling_7d_distance_m = 0.0
    rolling_7d_moving_time_s = 0.0
    rolling_7d_load = 0.0

    for ts, act in rides:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if dt > rolling_cutoff:
            rolling_7d_distance_m += float(act.get("distance") or 0)
            rolling_7d_moving_time_s += float(act.get("moving_time") or 0)
            rolling_7d_load += estimate_tss(act, ftp)

    return {
        "cycling_ctl": round(ctl, 2),
        "cycling_atl": round(atl, 2),
        "cycling_tsb": round(tsb, 2),
        "cycling_prev_ctl": round(prev_ctl, 2),
        "cycling_daily_tss": round(daily_tss, 2),
        "cycling_daily_kilojoules_today": round(daily_kj, 1),
        "cycling_last_activity_distance_m": last_distance_m,
        "cycling_last_activity_moving_time_seconds": last_moving_time_s,
        "cycling_last_activity_elapsed_time_seconds": last_elapsed_time_s,
        "cycling_last_activity_total_elevation_gain_m": last_elevation_m,
        "cycling_last_activity_average_watts": last_avg_watts,
        "cycling_last_activity_start_timestamp_seconds": last_start_ts,
        "cycling_weekly_distance_m": weekly_distance_m,
        "cycling_weekly_elevation_gain_m": weekly_elevation_m,
        "cycling_weekly_ride_count": weekly_count,
        "cycling_weekly_moving_time_seconds": weekly_moving_time_s,
        "cycling_weekly_tss": round(weekly_tss, 1),
        "cycling_weekly_longest_distance_m": weekly_longest_distance_m,
        "cycling_monthly_distance_m": monthly_distance_m,
        "cycling_monthly_elevation_gain_m": monthly_elevation_m,
        "cycling_monthly_ride_count": monthly_count,
        "cycling_rolling_7d_distance_m": rolling_7d_distance_m,
        "cycling_rolling_7d_moving_time_seconds": rolling_7d_moving_time_s,
        "cycling_rolling_7d_training_load_estimate": round(rolling_7d_load, 2),
    }
