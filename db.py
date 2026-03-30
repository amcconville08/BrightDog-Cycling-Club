"""
db.py - SQLite database layer for cycling club app.
All functions accept db_path so they can be called from any thread.
WAL mode enabled for better concurrent read performance.
"""
import sqlite3
import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
    conn = get_conn(db_path)
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT    NOT NULL UNIQUE,
                name        TEXT    NOT NULL,
                password_hash TEXT  NOT NULL,
                is_admin    INTEGER NOT NULL DEFAULT 0,
                ftp         REAL    NOT NULL DEFAULT 200,
                bmr         REAL    NOT NULL DEFAULT 1800,
                calorie_target REAL NOT NULL DEFAULT 2500,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS invites (
                token       TEXT    PRIMARY KEY,
                created_by_id INTEGER NOT NULL REFERENCES users(id),
                used_by_id  INTEGER REFERENCES users(id),
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strava_tokens (
                user_id       INTEGER PRIMARY KEY REFERENCES users(id),
                access_token  TEXT    NOT NULL,
                refresh_token TEXT    NOT NULL,
                expires_at    INTEGER NOT NULL,
                athlete_id    INTEGER
            );

            CREATE TABLE IF NOT EXISTS metrics_cache (
                user_id                                 INTEGER PRIMARY KEY REFERENCES users(id),
                ctl                                     REAL    DEFAULT 0,
                atl                                     REAL    DEFAULT 0,
                tsb                                     REAL    DEFAULT 0,
                daily_tss                               REAL    DEFAULT 0,
                daily_kj                                REAL    DEFAULT 0,
                last_distance_m                         REAL    DEFAULT 0,
                last_moving_time_s                      REAL    DEFAULT 0,
                last_elevation_m                        REAL    DEFAULT 0,
                last_avg_watts                          REAL    DEFAULT 0,
                last_start_ts                           REAL    DEFAULT 0,
                weekly_distance_m                       REAL    DEFAULT 0,
                weekly_elevation_m                      REAL    DEFAULT 0,
                weekly_count                            INTEGER DEFAULT 0,
                weekly_moving_time_s                    REAL    DEFAULT 0,
                monthly_distance_m                      REAL    DEFAULT 0,
                monthly_elevation_m                     REAL    DEFAULT 0,
                monthly_count                           INTEGER DEFAULT 0,
                rolling_7d_hours                        REAL    DEFAULT 0,
                rolling_7d_load                         REAL    DEFAULT 0,
                updated_at                              TEXT,
                weekly_tss                              REAL    DEFAULT 0,
                weekly_longest_distance_m               REAL    DEFAULT 0,
                prev_ctl                                REAL    DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS nutrition (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                date        TEXT    NOT NULL,
                calories    REAL    DEFAULT 0,
                carbs_g     REAL    DEFAULT 0,
                protein_g   REAL    DEFAULT 0,
                fat_g       REAL    DEFAULT 0,
                notes       TEXT    DEFAULT '',
                UNIQUE(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS coaching_cache (
                user_id     INTEGER PRIMARY KEY REFERENCES users(id),
                data        TEXT    NOT NULL DEFAULT '{}',
                computed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ftp_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL REFERENCES users(id),
                ftp_watts      REAL    NOT NULL,
                effective_date TEXT    NOT NULL,
                source         TEXT    NOT NULL DEFAULT 'manual',
                note           TEXT    DEFAULT '',
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS training_profile (
                user_id              INTEGER PRIMARY KEY REFERENCES users(id),
                goal                 TEXT    DEFAULT '',
                goal_custom          TEXT    DEFAULT '',
                preferred_days       TEXT    DEFAULT '',
                weekly_hours_target  REAL    DEFAULT 0,
                discipline           TEXT    DEFAULT 'road',
                target_event_date    TEXT    DEFAULT '',
                target_event_name    TEXT    DEFAULT '',
                weekly_rides_target  INTEGER DEFAULT 0,
                weekly_tss_target    REAL    DEFAULT 0,
                updated_at           TEXT
            );

            CREATE TABLE IF NOT EXISTS planned_workouts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL REFERENCES users(id),
                date                TEXT    NOT NULL,
                title               TEXT    NOT NULL,
                type                TEXT    NOT NULL DEFAULT 'endurance',
                target_duration_min INTEGER DEFAULT 0,
                target_tss          REAL    DEFAULT 0,
                notes               TEXT    DEFAULT '',
                target_power_zone   TEXT    DEFAULT '',
                target_hr_low       INTEGER DEFAULT 0,
                target_hr_high      INTEGER DEFAULT 0,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        _run_migrations(conn)
        _seed_ftp_history(conn)
    conn.close()


def _run_migrations(conn) -> None:
    """Safe column additions for existing databases."""
    migrations = [
        "ALTER TABLE metrics_cache ADD COLUMN weekly_tss REAL DEFAULT 0",
        "ALTER TABLE metrics_cache ADD COLUMN weekly_longest_distance_m REAL DEFAULT 0",
        "ALTER TABLE metrics_cache ADD COLUMN prev_ctl REAL DEFAULT 0",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # column already exists


def _seed_ftp_history(conn) -> None:
    """Create initial ftp_history entries from users.ftp for users with no history."""
    existing_ids = {row[0] for row in conn.execute(
        "SELECT DISTINCT user_id FROM ftp_history"
    ).fetchall()}
    users = conn.execute("SELECT id, ftp FROM users").fetchall()
    for u in users:
        if u["id"] not in existing_ids and u["ftp"]:
            conn.execute(
                "INSERT INTO ftp_history (user_id, ftp_watts, effective_date, source, note) "
                "VALUES (?, ?, date('now'), 'initial', 'Initial value')",
                (u["id"], u["ftp"])
            )


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def check_password(password: str, password_hash: str) -> bool:
    try:
        salt, h = password_hash.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def create_user(db_path: str, email: str, name: str, password: str, is_admin: bool = False,
                ftp: float = 200, bmr: float = 1800, calorie_target: float = 2500) -> int:
    conn = get_conn(db_path)
    pw_hash = hash_password(password)
    with conn:
        cur = conn.execute(
            "INSERT INTO users (email, name, password_hash, is_admin, ftp, bmr, calorie_target) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, name, pw_hash, int(is_admin), ftp, bmr, calorie_target)
        )
        uid = cur.lastrowid
        # Seed ftp_history for new user
        conn.execute(
            "INSERT INTO ftp_history (user_id, ftp_watts, effective_date, source, note) "
            "VALUES (?, ?, date('now'), 'initial', 'Initial value')",
            (uid, ftp)
        )
        return uid


def get_user_by_id(db_path: str, user_id: int):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_by_email(db_path: str, email: str):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return row


def get_all_users(db_path: str):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    conn.close()
    return rows


def update_user_settings(db_path: str, user_id: int, ftp: float = None, bmr: float = None,
                         calorie_target: float = None, name: str = None) -> None:
    conn = get_conn(db_path)
    updates = []
    params = []
    if ftp is not None:
        updates.append("ftp = ?")
        params.append(ftp)
    if bmr is not None:
        updates.append("bmr = ?")
        params.append(bmr)
    if calorie_target is not None:
        updates.append("calorie_target = ?")
        params.append(calorie_target)
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if not updates:
        conn.close()
        return
    params.append(user_id)
    with conn:
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    conn.close()


def verify_user_password(db_path: str, email: str, password: str):
    """Returns user row if credentials are valid, else None."""
    user = get_user_by_email(db_path, email)
    if user and check_password(password, user["password_hash"]):
        return user
    return None


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def create_invite(db_path: str, created_by_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO invites (token, created_by_id, expires_at) VALUES (?, ?, ?)",
            (token, created_by_id, expires_at)
        )
    conn.close()
    return token


def get_invite(db_path: str, token: str):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row


def use_invite(db_path: str, token: str, used_by_id: int) -> None:
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            "UPDATE invites SET used_by_id = ? WHERE token = ?",
            (used_by_id, token)
        )
    conn.close()


# ---------------------------------------------------------------------------
# Strava tokens
# ---------------------------------------------------------------------------

def save_strava_tokens(db_path: str, user_id: int, access_token: str,
                       refresh_token: str, expires_at: int, athlete_id: int = None) -> None:
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO strava_tokens (user_id, access_token, refresh_token, expires_at, athlete_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "access_token=excluded.access_token, "
            "refresh_token=excluded.refresh_token, "
            "expires_at=excluded.expires_at, "
            "athlete_id=COALESCE(excluded.athlete_id, strava_tokens.athlete_id)",
            (user_id, access_token, refresh_token, expires_at, athlete_id)
        )
    conn.close()


def get_strava_tokens(db_path: str, user_id: int):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM strava_tokens WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_users_with_strava(db_path: str):
    """Return all users who have connected Strava."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT u.* FROM users u "
        "INNER JOIN strava_tokens s ON s.user_id = u.id "
        "ORDER BY u.id"
    ).fetchall()
    conn.close()
    return rows


def delete_strava_tokens(db_path: str, user_id: int) -> None:
    """Remove Strava tokens for a user (disconnect)."""
    conn = get_conn(db_path)
    with conn:
        conn.execute("DELETE FROM strava_tokens WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM metrics_cache WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM coaching_cache WHERE user_id = ?", (user_id,))
    conn.close()


# ---------------------------------------------------------------------------
# FTP history
# ---------------------------------------------------------------------------

def add_ftp_entry(db_path: str, user_id: int, ftp_watts: float,
                  source: str = "manual", note: str = "") -> None:
    """Record a new FTP value in history. Also updates users.ftp."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO ftp_history (user_id, ftp_watts, effective_date, source, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, ftp_watts, today, source, note)
        )
        conn.execute("UPDATE users SET ftp = ? WHERE id = ?", (ftp_watts, user_id))
    conn.close()


def get_ftp_history(db_path: str, user_id: int, limit: int = 20):
    """Return FTP history entries newest-first."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM ftp_history WHERE user_id = ? ORDER BY effective_date DESC, id DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return rows


def get_current_ftp(db_path: str, user_id: int, fallback: float = 200.0) -> float:
    """Return the most recent FTP value, falling back to users.ftp."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT ftp_watts FROM ftp_history WHERE user_id = ? "
        "ORDER BY effective_date DESC, id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    if row:
        conn.close()
        return float(row[0])
    # fallback: users table
    u = conn.execute("SELECT ftp FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return float(u["ftp"]) if u else fallback


def get_previous_ftp(db_path: str, user_id: int):
    """Return the second-most-recent FTP entry (or None)."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM ftp_history WHERE user_id = ? "
        "ORDER BY effective_date DESC, id DESC LIMIT 2",
        (user_id,)
    ).fetchall()
    conn.close()
    if len(rows) >= 2:
        return rows[1]
    return None


# ---------------------------------------------------------------------------
# Training profile
# ---------------------------------------------------------------------------

def save_training_profile(db_path: str, user_id: int, goal: str = "", goal_custom: str = "",
                          preferred_days: str = "", weekly_hours_target: float = 0,
                          discipline: str = "road", target_event_date: str = "",
                          target_event_name: str = "", weekly_rides_target: int = 0,
                          weekly_tss_target: float = 0) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            """INSERT INTO training_profile
               (user_id, goal, goal_custom, preferred_days, weekly_hours_target,
                discipline, target_event_date, target_event_name,
                weekly_rides_target, weekly_tss_target, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 goal=excluded.goal, goal_custom=excluded.goal_custom,
                 preferred_days=excluded.preferred_days,
                 weekly_hours_target=excluded.weekly_hours_target,
                 discipline=excluded.discipline,
                 target_event_date=excluded.target_event_date,
                 target_event_name=excluded.target_event_name,
                 weekly_rides_target=excluded.weekly_rides_target,
                 weekly_tss_target=excluded.weekly_tss_target,
                 updated_at=excluded.updated_at""",
            (user_id, goal, goal_custom, preferred_days, weekly_hours_target,
             discipline, target_event_date, target_event_name,
             weekly_rides_target, weekly_tss_target, now)
        )
    conn.close()


def get_training_profile(db_path: str, user_id: int) -> dict:
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM training_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Planned workouts
# ---------------------------------------------------------------------------

def save_planned_workout(db_path: str, user_id: int, date: str, title: str,
                         type_: str = "endurance", target_duration_min: int = 0,
                         target_tss: float = 0, notes: str = "",
                         target_power_zone: str = "",
                         target_hr_low: int = 0, target_hr_high: int = 0) -> int:
    conn = get_conn(db_path)
    with conn:
        cur = conn.execute(
            """INSERT INTO planned_workouts
               (user_id, date, title, type, target_duration_min, target_tss,
                notes, target_power_zone, target_hr_low, target_hr_high)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, date, title, type_, target_duration_min, target_tss,
             notes, target_power_zone, target_hr_low, target_hr_high)
        )
        return cur.lastrowid
    conn.close()


def get_planned_workouts(db_path: str, user_id: int,
                         from_date: str = None, to_date: str = None):
    conn = get_conn(db_path)
    if from_date and to_date:
        rows = conn.execute(
            "SELECT * FROM planned_workouts WHERE user_id = ? AND date BETWEEN ? AND ? "
            "ORDER BY date ASC",
            (user_id, from_date, to_date)
        ).fetchall()
    elif from_date:
        rows = conn.execute(
            "SELECT * FROM planned_workouts WHERE user_id = ? AND date >= ? ORDER BY date ASC",
            (user_id, from_date)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM planned_workouts WHERE user_id = ? ORDER BY date ASC",
            (user_id,)
        ).fetchall()
    conn.close()
    return rows


def get_today_workout(db_path: str, user_id: int, date_str: str):
    """Return the first planned workout for today (or None)."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM planned_workouts WHERE user_id = ? AND date = ? ORDER BY id ASC LIMIT 1",
        (user_id, date_str)
    ).fetchone()
    conn.close()
    return row


def delete_planned_workout(db_path: str, workout_id: int, user_id: int) -> None:
    """Delete a planned workout (user_id guard prevents cross-user deletion)."""
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            "DELETE FROM planned_workouts WHERE id = ? AND user_id = ?",
            (workout_id, user_id)
        )
    conn.close()


# ---------------------------------------------------------------------------
# Metrics cache
# ---------------------------------------------------------------------------

def save_metrics_cache(db_path: str, user_id: int, values: dict) -> None:
    conn = get_conn(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute(
            """INSERT INTO metrics_cache (
                user_id, ctl, atl, tsb, daily_tss, daily_kj,
                last_distance_m, last_moving_time_s, last_elevation_m,
                last_avg_watts, last_start_ts,
                weekly_distance_m, weekly_elevation_m, weekly_count, weekly_moving_time_s,
                monthly_distance_m, monthly_elevation_m, monthly_count,
                rolling_7d_hours, rolling_7d_load,
                weekly_tss, weekly_longest_distance_m, prev_ctl,
                updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                ctl=excluded.ctl, atl=excluded.atl, tsb=excluded.tsb,
                daily_tss=excluded.daily_tss, daily_kj=excluded.daily_kj,
                last_distance_m=excluded.last_distance_m,
                last_moving_time_s=excluded.last_moving_time_s,
                last_elevation_m=excluded.last_elevation_m,
                last_avg_watts=excluded.last_avg_watts,
                last_start_ts=excluded.last_start_ts,
                weekly_distance_m=excluded.weekly_distance_m,
                weekly_elevation_m=excluded.weekly_elevation_m,
                weekly_count=excluded.weekly_count,
                weekly_moving_time_s=excluded.weekly_moving_time_s,
                monthly_distance_m=excluded.monthly_distance_m,
                monthly_elevation_m=excluded.monthly_elevation_m,
                monthly_count=excluded.monthly_count,
                rolling_7d_hours=excluded.rolling_7d_hours,
                rolling_7d_load=excluded.rolling_7d_load,
                weekly_tss=excluded.weekly_tss,
                weekly_longest_distance_m=excluded.weekly_longest_distance_m,
                prev_ctl=excluded.prev_ctl,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                values.get("cycling_ctl", 0),
                values.get("cycling_atl", 0),
                values.get("cycling_tsb", 0),
                values.get("cycling_daily_tss", 0),
                values.get("cycling_daily_kilojoules_today", 0),
                values.get("cycling_last_activity_distance_m", 0),
                values.get("cycling_last_activity_moving_time_seconds", 0),
                values.get("cycling_last_activity_total_elevation_gain_m", 0),
                values.get("cycling_last_activity_average_watts", 0),
                values.get("cycling_last_activity_start_timestamp_seconds", 0),
                values.get("cycling_weekly_distance_m", 0),
                values.get("cycling_weekly_elevation_gain_m", 0),
                values.get("cycling_weekly_ride_count", 0),
                values.get("cycling_weekly_moving_time_seconds", 0),
                values.get("cycling_monthly_distance_m", 0),
                values.get("cycling_monthly_elevation_gain_m", 0),
                values.get("cycling_monthly_ride_count", 0),
                values.get("cycling_rolling_7d_moving_time_seconds", 0) / 3600.0,
                values.get("cycling_rolling_7d_training_load_estimate", 0),
                values.get("cycling_weekly_tss", 0),
                values.get("cycling_weekly_longest_distance_m", 0),
                values.get("cycling_prev_ctl", 0),
                now,
            )
        )
    conn.close()


def get_metrics_cache(db_path: str, user_id: int):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM metrics_cache WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

def get_leaderboard(db_path: str):
    """
    Return a list of dicts with each user's weekly stats for leaderboard display.
    Only includes users who have a metrics_cache entry.
    """
    conn = get_conn(db_path)
    rows = conn.execute(
        """SELECT u.id, u.name,
                  COALESCE(m.weekly_distance_m, 0)   AS weekly_distance_m,
                  COALESCE(m.weekly_moving_time_s, 0) AS weekly_moving_time_s,
                  COALESCE(m.weekly_elevation_m, 0)   AS weekly_elevation_m,
                  COALESCE(m.weekly_tss, 0)            AS weekly_tss,
                  COALESCE(m.weekly_count, 0)          AS weekly_count,
                  COALESCE(m.weekly_longest_distance_m, 0) AS weekly_longest_distance_m,
                  COALESCE(m.ctl, 0) AS ctl,
                  COALESCE(m.tsb, 0) AS tsb
           FROM users u
           LEFT JOIN metrics_cache m ON m.user_id = u.id
           ORDER BY u.name""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Nutrition
# ---------------------------------------------------------------------------

def save_nutrition(db_path: str, user_id: int, date: str, calories: float,
                   carbs_g: float, protein_g: float, fat_g: float, notes: str = "") -> None:
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            """INSERT INTO nutrition (user_id, date, calories, carbs_g, protein_g, fat_g, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET
                 calories=excluded.calories, carbs_g=excluded.carbs_g,
                 protein_g=excluded.protein_g, fat_g=excluded.fat_g,
                 notes=excluded.notes""",
            (user_id, date, calories, carbs_g, protein_g, fat_g, notes)
        )
    conn.close()


def get_nutrition(db_path: str, user_id: int, date: str):
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM nutrition WHERE user_id = ? AND date = ?",
        (user_id, date)
    ).fetchone()
    conn.close()
    return row


def get_nutrition_history(db_path: str, user_id: int, days: int = 7):
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM nutrition WHERE user_id = ? ORDER BY date DESC LIMIT ?",
        (user_id, days)
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Coaching cache
# ---------------------------------------------------------------------------

def save_coaching_cache(db_path: str, user_id: int, data: dict) -> None:
    import json
    conn = get_conn(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute(
            """INSERT INTO coaching_cache (user_id, data, computed_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, computed_at=excluded.computed_at""",
            (user_id, json.dumps(data), now)
        )
    conn.close()


def get_coaching_cache(db_path: str, user_id: int):
    import json
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM coaching_cache WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["data"])
        except Exception:
            return {}
    return {}
