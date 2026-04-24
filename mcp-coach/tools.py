"""
tools.py — Read-only tool functions for mcp-coach.
Each function fetches structured data from the shared club.db.
These are the grounding layer — Groq never computes these values.
"""
import sqlite3
from datetime import date, timedelta


def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _monday_of_week(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def get_today_coaching_brief(db_path: str, user_id: int) -> dict:
    import json as _json
    c = _conn(db_path)
    row = c.execute(
        "SELECT data FROM coaching_cache WHERE user_id=?", (user_id,)
    ).fetchone()
    c.close()
    if not row:
        return {}
    try:
        return _json.loads(row["data"])
    except Exception:
        return {}


def get_readiness_summary(db_path: str, user_id: int) -> dict:
    brief = get_today_coaching_brief(db_path, user_id)

    # Pull prev_ctl from metrics_cache (where it lives as a column)
    prev_ctl = 0.0
    try:
        c = _conn(db_path)
        row = c.execute(
            "SELECT prev_ctl FROM metrics_cache WHERE user_id=?", (user_id,)
        ).fetchone()
        c.close()
        if row and row["prev_ctl"]:
            prev_ctl = float(row["prev_ctl"])
    except Exception:
        pass

    return {
        "ctl":            round(float(brief.get("ctl", 0)), 1),
        "atl":            round(float(brief.get("atl", 0)), 1),
        "tsb":            round(float(brief.get("tsb", 0)), 1),
        "prev_ctl":       round(prev_ctl, 1),
        "classification": brief.get("classification", "—"),
        "readiness":      brief.get("readiness_score", 0),
        "ride_style":     brief.get("ride_style", ""),
        "ride_rationale": brief.get("ride_style_rationale", ""),
        "ride_hint":      brief.get("ride_hint", ""),
        "suggested_duration_minutes": brief.get("suggested_duration_minutes", 0),
        "suggested_tss":  brief.get("suggested_tss", 0),
    }


def get_recent_rides(db_path: str, user_id: int, days: int = 14) -> list:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    c = _conn(db_path)
    rows = c.execute(
        """SELECT date, name, moving_time_s, distance_m,
                  tss, avg_watts, np_watts, elevation_m
           FROM activity_log
           WHERE user_id=? AND date>=?
           ORDER BY date DESC LIMIT 20""",
        (user_id, cutoff),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_last_ride(db_path: str, user_id: int) -> dict:
    rides = get_recent_rides(db_path, user_id, days=7)
    return rides[0] if rides else {}


def get_today_rides(db_path: str, user_id: int) -> list:
    """Return all rides logged for today."""
    today = date.today().isoformat()
    c = _conn(db_path)
    rows = c.execute(
        """SELECT date, name, moving_time_s, distance_m,
                  tss, avg_watts, np_watts, elevation_m
           FROM activity_log
           WHERE user_id=? AND date=?
           ORDER BY id DESC""",
        (user_id, today),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_previous_week_rides(db_path: str, user_id: int) -> list:
    """Return all rides from the previous calendar week (Mon–Sun)."""
    today   = date.today()
    this_monday = _monday_of_week(today)
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = this_monday - timedelta(days=1)
    c = _conn(db_path)
    rows = c.execute(
        """SELECT date, name, moving_time_s, distance_m,
                  tss, avg_watts, np_watts, elevation_m
           FROM activity_log
           WHERE user_id=? AND date>=? AND date<=?
           ORDER BY date ASC""",
        (user_id, prev_monday.isoformat(), prev_sunday.isoformat()),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_previous_week_summary(db_path: str, user_id: int) -> dict:
    """Aggregate stats for the previous calendar week."""
    rides = get_previous_week_rides(db_path, user_id)
    if not rides:
        return {"rides": 0, "hours": 0.0, "tss": 0.0, "distance_km": 0.0, "elevation_m": 0.0}
    total_tss   = sum(float(r["tss"] or 0)               for r in rides)
    total_secs  = sum(float(r["moving_time_s"] or 0)      for r in rides)
    total_dist  = sum(float(r["distance_m"] or 0)         for r in rides)
    total_elev  = sum(float(r["elevation_m"] or 0)        for r in rides)
    return {
        "rides":       len(rides),
        "hours":       round(total_secs / 3600, 1),
        "tss":         round(total_tss, 0),
        "distance_km": round(total_dist / 1000, 1),
        "elevation_m": round(total_elev, 0),
    }


def get_week_summary(db_path: str, user_id: int) -> dict:
    c = _conn(db_path)
    row = c.execute(
        "SELECT * FROM metrics_cache WHERE user_id=?", (user_id,)
    ).fetchone()
    c.close()
    if not row:
        return {}
    r = dict(row)
    return {
        "weekly_tss":         round(float(r.get("weekly_tss", 0)), 0),
        "weekly_hours":       round(float(r.get("weekly_moving_time_s", 0)) / 3600, 1),
        "weekly_rides":       int(r.get("weekly_count", 0)),
        "weekly_distance_km": round(float(r.get("weekly_distance_m", 0)) / 1000, 1),
        "weekly_elevation_m": round(float(r.get("weekly_elevation_m", 0)), 0),
        "rolling_7d_hours":   round(float(r.get("rolling_7d_hours", 0)), 1),
    }


def get_training_goal(db_path: str, user_id: int) -> dict:
    c = _conn(db_path)
    row = c.execute(
        "SELECT * FROM training_profile WHERE user_id=?", (user_id,)
    ).fetchone()
    c.close()
    if not row:
        return {}
    r = dict(row)
    goal = r.get("goal_custom") or r.get("goal", "")
    return {
        "goal":                goal,
        "weekly_hours_target": r.get("weekly_hours_target", 0),
        "weekly_tss_target":   r.get("weekly_tss_target", 0),
        "target_event_date":   r.get("target_event_date", ""),
        "target_event_name":   r.get("target_event_name", ""),
        "long_ride_day":       r.get("preferred_days", ""),
    }


def get_current_ftp(db_path: str, user_id: int) -> dict:
    c = _conn(db_path)
    row = c.execute(
        "SELECT ftp_watts, source FROM ftp_history "
        "WHERE user_id=? ORDER BY effective_date DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        c.close()
        return {"ftp": float(row["ftp_watts"]), "source": row["source"]}
    u = c.execute("SELECT ftp FROM users WHERE id=?", (user_id,)).fetchone()
    c.close()
    return {"ftp": float(u["ftp"]) if u and u["ftp"] else 200.0, "source": "users"}


def get_current_zones(db_path: str, user_id: int) -> dict:
    ftp_data = get_current_ftp(db_path, user_id)
    ftp = ftp_data["ftp"]
    return {
        "ftp":    ftp,
        "source": ftp_data["source"],
        "Z1_Active_Recovery": (0,              round(ftp * 0.55)),
        "Z2_Endurance":       (round(ftp*0.55), round(ftp * 0.75)),
        "Z3_Tempo":           (round(ftp*0.75), round(ftp * 0.90)),
        "Z4_Threshold":       (round(ftp*0.90), round(ftp * 1.05)),
        "Z5_VO2Max":          (round(ftp*1.05), round(ftp * 1.20)),
        "Z6_Anaerobic":       (round(ftp*1.20), round(ftp * 1.50)),
        "Z7_Neuromuscular":   (round(ftp*1.50), 9999),
    }


def estimate_ftp_candidate(db_path: str, user_id: int):
    """Best NP from 20–70 min power-meter rides × duration factor."""
    c = _conn(db_path)
    rows = c.execute(
        """SELECT moving_time_s, np_watts FROM activity_log
           WHERE user_id=? AND np_watts>0 AND moving_time_s BETWEEN 1200 AND 4200
           ORDER BY date DESC LIMIT 20""",
        (user_id,),
    ).fetchall()
    c.close()
    candidates = []
    for r in rows:
        np = float(r["np_watts"])
        if np < 80:
            continue
        factor = 0.95 + 0.05 * min(1.0, (float(r["moving_time_s"]) - 1200) / 2400)
        candidates.append(np * factor)
    if not candidates:
        return None
    candidates.sort()
    idx = max(0, int(len(candidates) * 0.9) - 1)
    return int(round(candidates[idx]))


def get_weekly_outlook(db_path: str, user_id: int) -> dict:
    """Return the weekly_outlook block from the coaching cache, or empty dict."""
    brief = get_today_coaching_brief(db_path, user_id)
    return brief.get("weekly_outlook") or {}


def get_athlete_profile(db_path: str, user_id: int) -> dict:
    c = _conn(db_path)
    row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    c.close()
    if not row:
        return {}
    return {
        "name":           row["name"],
        "ftp":            row["ftp"],
        "bmr":            row["bmr"],
        "calorie_target": row["calorie_target"],
    }
