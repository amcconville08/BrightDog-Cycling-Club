"""
app.py - Cycling Club Flask web application.
All HTML is inline. No templates directory.
Dark theme, mobile-first, Tailwind CDN.
"""
import os
import json
import logging
import functools
import urllib.request
import urllib.error
from datetime import datetime, timezone, date

from flask import (
    Flask,
    session,
    redirect,
    url_for,
    request,
    make_response,
    g,
)

import db
import strava
import metrics as metrics_mod
import coaching as coach_mod
from poller import Poller

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("cycling-club.app")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-please-change")
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_REDIRECT_URI = os.environ.get(
    "STRAVA_REDIRECT_URI", "http://localhost:9206/strava/callback"
)
DB_PATH = os.environ.get("DB_PATH", "/data/club.db")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))
MCP_COACH_URL = os.environ.get("MCP_COACH_URL", "http://mcp-coach:9207")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Initialise DB on import (safe to call multiple times)
db.init_db(DB_PATH)

# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def fmt_dist(m: float) -> str:
    """Format metres to km string: '89.2 km'"""
    if not m:
        return "0.0 km"
    return f"{m / 1000:.1f} km"


def fmt_time(s: float) -> str:
    """Format seconds to h:mm string: '2h 45m'"""
    if not s:
        return "0h 00m"
    s = int(s)
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m:02d}m"


def fmt_date(ts: float) -> str:
    """Format unix timestamp to '28 Mar'"""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%-d %b")
    except Exception:
        return "—"


def fmt_elev(m: float) -> str:
    """Format elevation in metres: '1 234 m'"""
    if not m:
        return "0 m"
    return f"{int(m):,} m".replace(",", " ")


def status_color(classification: str) -> str:
    """Map classification to Tailwind text colour class."""
    mapping = {
        "Fresh": "text-green-400",
        "Balanced": "text-cyan-400",
        "Fatigued": "text-yellow-400",
        "Very Fatigued": "text-orange-400",
        "Overreaching": "text-red-400",
    }
    return mapping.get(classification, "text-gray-400")


def status_bg(classification: str) -> str:
    """Map classification to Tailwind bg colour class."""
    mapping = {
        "Fresh": "bg-green-900/30",
        "Balanced": "bg-cyan-900/30",
        "Fatigued": "bg-yellow-900/30",
        "Very Fatigued": "bg-orange-900/30",
        "Overreaching": "bg-red-900/30",
    }
    return mapping.get(classification, "bg-gray-800/30")


def status_hex(classification: str) -> str:
    return coach_mod.STATUS_COLOR.get(classification, "#9e9e9e")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.get_user_by_id(DB_PATH, uid)


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        g.user = user
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["is_admin"]:
            return _html_response(_page("Access Denied", "<p class='text-red-400'>Admin only.</p>", user), 403)
        g.user = user
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Shared HTML components
# ---------------------------------------------------------------------------

TAILWIND_CDN = '<script src="https://cdn.tailwindcss.com"></script>'

def _head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en" class="h-full">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title} — Cycling Club</title>
  {TAILWIND_CDN}
  <style>
    body {{ background-color: #030712; }}
    .bottom-safe {{ padding-bottom: env(safe-area-inset-bottom, 0px); }}
  </style>
</head>
<body class="min-h-full text-gray-100 bg-gray-950">
"""

def _foot() -> str:
    return "\n</body>\n</html>"


def _nav(current: str, user) -> str:
    """Bottom navigation bar."""
    if not user:
        return ""

    def _tab(href, label, icon, key):
        active = "text-cyan-400 border-t-2 border-cyan-400" if current == key else "text-gray-500"
        return f"""<a href="{href}" class="flex flex-col items-center flex-1 py-2 {active} text-xs font-medium">
          <span class="text-xl mb-0.5">{icon}</span>{label}
        </a>"""

    admin_tab = ""
    if user["is_admin"]:
        admin_tab = _tab(url_for("admin"), "Admin", "👥", "admin")

    return f"""
<nav class="fixed bottom-0 left-0 right-0 bg-gray-900 border-t border-gray-800 flex bottom-safe z-50">
  {_tab(url_for("dashboard"), "Home", "🏠", "home")}
  {_tab(url_for("coaching"), "Coach", "📊", "coach")}
  {_tab(url_for("nutrition"), "Food", "🥗", "food")}
  {_tab(url_for("settings"), "Settings", "⚙️", "settings")}
  {admin_tab}
</nav>
<div class="h-16"></div>
"""


def _page(title: str, body: str, user=None, current: str = "") -> str:
    return (
        _head(title)
        + f"""
<div class="max-w-lg mx-auto px-4 pt-6 pb-4">
  <h1 class="text-lg font-bold text-gray-100 mb-4">{title}</h1>
  {body}
</div>
{_nav(current, user)}
"""
        + _foot()
    )


def _html_response(html: str, status: int = 200):
    resp = make_response(html, status)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


def _card(title: str, content: str, extra_class: str = "") -> str:
    return f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3 {extra_class}">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">{title}</p>
  {content}
</div>
"""


def _metric_pill(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<p class="text-xs text-gray-500 mt-0.5">{sub}</p>' if sub else ""
    return f"""
<div class="flex flex-col items-center">
  <p class="text-2xl font-bold text-gray-100">{value}</p>
  <p class="text-xs text-gray-400 mt-0.5">{label}</p>
  {sub_html}
</div>
"""


def _flash_msg(msg: str, kind: str = "error") -> str:
    colour = "bg-red-900/50 text-red-300 border border-red-700" if kind == "error" else "bg-green-900/50 text-green-300 border border-green-700"
    return f'<div class="rounded-lg px-4 py-3 mb-4 text-sm {colour}">{msg}</div>' if msg else ""


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    user = get_current_user()
    if user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.verify_user_password(DB_PATH, email, password)
        if user:
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        error = "Invalid email or password."

    body = f"""
{_flash_msg(error)}
<form method="post" class="space-y-4">
  <div>
    <label class="block text-sm text-gray-400 mb-1">Email</label>
    <input name="email" type="email" required autocomplete="email"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 focus:outline-none focus:border-cyan-500"/>
  </div>
  <div>
    <label class="block text-sm text-gray-400 mb-1">Password</label>
    <input name="password" type="password" required autocomplete="current-password"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 focus:outline-none focus:border-cyan-500"/>
  </div>
  <button type="submit"
    class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2.5 rounded-lg transition-colors">
    Sign in
  </button>
</form>
"""
    return _html_response(_page("Sign in", body))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/register/<token>", methods=["GET", "POST"])
def register(token: str):
    invite = db.get_invite(DB_PATH, token)
    error = ""
    success = ""

    if not invite:
        return _html_response(_page("Invalid Invite", "<p class='text-red-400'>This invite link is invalid.</p>"))

    if invite["used_by_id"]:
        return _html_response(_page("Invalid Invite", "<p class='text-red-400'>This invite has already been used.</p>"))

    # Check expiry
    try:
        expires = datetime.strptime(invite["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            return _html_response(_page("Expired Invite", "<p class='text-red-400'>This invite link has expired.</p>"))
    except Exception:
        pass

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not email or not name or not password:
            error = "All fields are required."
        elif password != confirm:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif db.get_user_by_email(DB_PATH, email):
            error = "That email is already registered."
        else:
            is_admin = ADMIN_EMAIL and email == ADMIN_EMAIL.lower()
            try:
                uid = db.create_user(DB_PATH, email, name, password, is_admin=is_admin)
                db.use_invite(DB_PATH, token, uid)
                session["user_id"] = uid
                return redirect(url_for("dashboard"))
            except Exception as exc:
                log.error("Registration error: %s", exc)
                error = "Registration failed. Please try again."

    body = f"""
{_flash_msg(error)}
<p class="text-sm text-gray-400 mb-4">You've been invited to join the cycling club.</p>
<form method="post" class="space-y-4">
  <div>
    <label class="block text-sm text-gray-400 mb-1">Full name</label>
    <input name="name" type="text" required autocomplete="name"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 focus:outline-none focus:border-cyan-500"/>
  </div>
  <div>
    <label class="block text-sm text-gray-400 mb-1">Email</label>
    <input name="email" type="email" required autocomplete="email"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 focus:outline-none focus:border-cyan-500"/>
  </div>
  <div>
    <label class="block text-sm text-gray-400 mb-1">Password</label>
    <input name="password" type="password" required autocomplete="new-password"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 focus:outline-none focus:border-cyan-500"/>
  </div>
  <div>
    <label class="block text-sm text-gray-400 mb-1">Confirm password</label>
    <input name="confirm" type="password" required autocomplete="new-password"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 focus:outline-none focus:border-cyan-500"/>
  </div>
  <button type="submit"
    class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2.5 rounded-lg transition-colors">
    Create account
  </button>
</form>
"""
    return _html_response(_page("Create Account", body))


# ---------------------------------------------------------------------------
# Routes: Strava OAuth
# ---------------------------------------------------------------------------

@app.route("/connect-strava")
@login_required
def connect_strava():
    if not STRAVA_CLIENT_ID:
        return _html_response(_page("Error", "<p class='text-red-400'>Strava client ID not configured.</p>", g.user, "settings"))
    url = strava.get_auth_url(
        STRAVA_CLIENT_ID,
        STRAVA_REDIRECT_URI,
        scopes="read,activity:read_all",
        state=str(g.user["id"]),
    )
    return redirect(url)


@app.route("/strava/callback")
def strava_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state")

    if error:
        log.warning("Strava OAuth error: %s", error)
        return redirect(url_for("settings") + "?msg=strava_denied")

    if not code:
        return redirect(url_for("settings") + "?msg=strava_error")

    # Identify user: prefer session, fall back to state param
    uid = session.get("user_id")
    if not uid and state:
        try:
            uid = int(state)
        except ValueError:
            pass

    if not uid:
        return redirect(url_for("login"))

    try:
        token_data = strava.exchange_code(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, code)
        athlete = token_data.get("athlete", {})
        db.save_strava_tokens(
            DB_PATH,
            uid,
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=token_data["expires_at"],
            athlete_id=athlete.get("id"),
        )
        log.info("Strava connected for user %s (athlete %s)", uid, athlete.get("id"))
    except Exception as exc:
        log.error("Strava token exchange failed: %s", exc)
        return redirect(url_for("settings") + "?msg=strava_error")

    # Set session if not already set
    if not session.get("user_id"):
        session["user_id"] = uid

    return redirect(url_for("settings") + "?msg=strava_ok")


# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    user = g.user
    mc = db.get_metrics_cache(DB_PATH, user["id"])
    coaching = db.get_coaching_cache(DB_PATH, user["id"])

    has_strava = db.get_strava_tokens(DB_PATH, user["id"]) is not None
    classification = coaching.get("classification", "—")
    readiness = coaching.get("readiness_score", 0)
    headline = coaching.get("headline", "Connect Strava to get started.")

    # Readiness card
    sc = status_color(classification)
    sb = status_bg(classification)
    readiness_card = f"""
<div class="bg-gray-900 rounded-xl p-5 mb-3 flex items-center gap-4">
  <div class="relative w-20 h-20 flex-shrink-0">
    <svg class="w-20 h-20 -rotate-90" viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="15.9" fill="none" stroke="#1f2937" stroke-width="3"/>
      <circle cx="18" cy="18" r="15.9" fill="none"
        stroke="{status_hex(classification)}" stroke-width="3"
        stroke-dasharray="{readiness:.0f} 100"
        stroke-linecap="round"/>
    </svg>
    <div class="absolute inset-0 flex items-center justify-center">
      <span class="text-lg font-bold text-gray-100">{readiness:.0f}</span>
    </div>
  </div>
  <div class="flex-1 min-w-0">
    <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Readiness</p>
    <span class="inline-block px-2 py-0.5 rounded-full text-xs font-semibold {sc} {sb} mb-1">{classification}</span>
    <p class="text-sm text-gray-300 leading-snug">{headline}</p>
  </div>
</div>
"""

    # Fitness metrics row
    ctl = coaching.get("ctl", 0)
    atl = coaching.get("atl", 0)
    tsb = coaching.get("tsb", 0)
    tsb_color = "text-green-400" if tsb >= 0 else "text-red-400"
    tsb_sign = "+" if tsb >= 0 else ""

    metrics_card = _card("Training Load", f"""
<div class="grid grid-cols-3 gap-2 text-center">
  <div>
    <p class="text-xl font-bold text-blue-400">{ctl:.0f}</p>
    <p class="text-xs text-gray-500">Fitness</p>
  </div>
  <div>
    <p class="text-xl font-bold text-purple-400">{atl:.0f}</p>
    <p class="text-xs text-gray-500">Fatigue</p>
  </div>
  <div>
    <p class="text-xl font-bold {tsb_color}">{tsb_sign}{tsb:.0f}</p>
    <p class="text-xs text-gray-500">Form</p>
  </div>
</div>
""")

    # Last ride card
    if mc:
        last_dist = fmt_dist(mc["last_distance_m"])
        last_time = fmt_time(mc["last_moving_time_s"])
        last_elev = fmt_elev(mc["last_elevation_m"])
        last_date = fmt_date(mc["last_start_ts"])
        last_watts = f'{mc["last_avg_watts"]:.0f} W' if mc["last_avg_watts"] else "—"
        last_ride_body = f"""
<div class="flex items-baseline justify-between mb-1">
  <p class="text-2xl font-bold text-gray-100">{last_dist}</p>
  <p class="text-sm text-gray-400">{last_date}</p>
</div>
<div class="flex gap-4 text-sm text-gray-400">
  <span>⏱ {last_time}</span>
  <span>⛰ {last_elev}</span>
  <span>⚡ {last_watts}</span>
</div>
"""
    else:
        last_ride_body = '<p class="text-sm text-gray-500">No ride data yet.</p>'

    last_ride_card = _card("Last Ride", last_ride_body)

    # This week card
    if mc:
        week_body = f"""
<div class="grid grid-cols-3 gap-2 text-center">
  <div>
    <p class="text-lg font-bold text-gray-100">{fmt_dist(mc["weekly_distance_m"])}</p>
    <p class="text-xs text-gray-500">Distance</p>
  </div>
  <div>
    <p class="text-lg font-bold text-gray-100">{fmt_time(mc["weekly_moving_time_s"])}</p>
    <p class="text-xs text-gray-500">Moving time</p>
  </div>
  <div>
    <p class="text-lg font-bold text-gray-100">{mc["weekly_count"]}</p>
    <p class="text-xs text-gray-500">Rides</p>
  </div>
</div>
"""
    else:
        week_body = '<p class="text-sm text-gray-500">No data.</p>'

    week_card = _card("This Week", week_body)

    # This month card
    if mc:
        month_body = f"""
<div class="grid grid-cols-2 gap-2 text-center">
  <div>
    <p class="text-lg font-bold text-gray-100">{fmt_dist(mc["monthly_distance_m"])}</p>
    <p class="text-xs text-gray-500">Distance</p>
  </div>
  <div>
    <p class="text-lg font-bold text-gray-100">{mc["monthly_count"]}</p>
    <p class="text-xs text-gray-500">Rides</p>
  </div>
</div>
"""
    else:
        month_body = '<p class="text-sm text-gray-500">No data.</p>'

    month_card = _card("This Month", month_body)

    # Strava connect prompt
    strava_prompt = ""
    if not has_strava:
        strava_prompt = f"""
<div class="bg-orange-900/20 border border-orange-700/50 rounded-xl p-4 mb-3">
  <p class="text-sm text-orange-300 mb-2">Connect Strava to see your fitness metrics.</p>
  <a href="{url_for('connect_strava')}"
     class="inline-block bg-orange-500 hover:bg-orange-400 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors">
    Connect Strava
  </a>
</div>
"""

    # Updated at
    updated_at = ""
    if mc and mc["updated_at"]:
        ts_str = str(mc["updated_at"])[:16]  # "2026-04-20 12:34"
        updated_at = f'<p class="text-xs text-gray-600 text-center mt-2">Last synced: {ts_str} UTC</p>'

    body = strava_prompt + readiness_card + metrics_card + last_ride_card + week_card + month_card + updated_at

    html = (
        _head(f"Dashboard — {user['name']}")
        + f"""
<div class="max-w-lg mx-auto px-4 pt-6 pb-4">
  <div class="flex items-center justify-between mb-4">
    <div>
      <h1 class="text-lg font-bold text-gray-100">Hi, {user['name'].split()[0]}</h1>
      <p class="text-xs text-gray-500">{datetime.now(timezone.utc).strftime('%A %-d %B')}</p>
    </div>
    <a href="{url_for('logout')}" class="text-xs text-gray-600 hover:text-gray-400">Sign out</a>
  </div>
  {body}
</div>
{_nav("home", user)}
"""
        + _foot()
    )
    return _html_response(html)


# ---------------------------------------------------------------------------
# Routes: Coaching
# ---------------------------------------------------------------------------

@app.route("/coaching")
@login_required
def coaching():
    user = g.user
    coaching_data = db.get_coaching_cache(DB_PATH, user["id"])
    has_strava = db.get_strava_tokens(DB_PATH, user["id"]) is not None

    if not coaching_data or not has_strava:
        no_data = ""
        if not has_strava:
            no_data = f"""
<div class="bg-orange-900/20 border border-orange-700/50 rounded-xl p-4 mb-4">
  <p class="text-sm text-orange-300 mb-2">Connect Strava to get coaching recommendations.</p>
  <a href="{url_for('connect_strava')}"
     class="inline-block bg-orange-500 hover:bg-orange-400 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors">
    Connect Strava
  </a>
</div>
"""
        else:
            no_data = '<p class="text-sm text-gray-500">Coaching data not yet available. Check back after the next sync.</p>'

        return _html_response(_page("Coaching", no_data, user, "coach"))

    classification = coaching_data.get("classification", "—")
    readiness = coaching_data.get("readiness_score", 0)
    headline = coaching_data.get("headline", "")
    explanation = coaching_data.get("explanation", "")
    ctl = coaching_data.get("ctl", 0)
    atl = coaching_data.get("atl", 0)
    tsb = coaching_data.get("tsb", 0)
    ratio = coaching_data.get("fatigue_ratio", 0)
    tss_today = coaching_data.get("tss_today", 0)
    risk_flag = coaching_data.get("risk_flag")
    flags = coaching_data.get("flags") or []
    insights = coaching_data.get("insights") or []

    # Onboarding nudge for users with very little data (CTL < 10)
    new_user_notice = ""
    if ctl < 10:
        new_user_notice = """
<div class="bg-blue-900/20 border border-blue-700/40 rounded-xl px-4 py-3 mb-3">
  <p class="text-xs text-blue-300 leading-relaxed">
    <strong>Building your model</strong> — coaching insights improve as more rides are synced.
    Expect accurate recommendations after 4–6 weeks of activity.
  </p>
</div>
"""

    sc = status_color(classification)
    sb = status_bg(classification)
    tsb_sign = "+" if tsb >= 0 else ""
    tsb_color = "text-green-400" if tsb >= 0 else "text-red-400"

    # Status badge header
    status_section = f"""
<div class="bg-gray-900 rounded-xl p-5 mb-3">
  <div class="flex items-center gap-3 mb-3">
    <div class="relative w-16 h-16 flex-shrink-0">
      <svg class="w-16 h-16 -rotate-90" viewBox="0 0 36 36">
        <circle cx="18" cy="18" r="15.9" fill="none" stroke="#1f2937" stroke-width="3"/>
        <circle cx="18" cy="18" r="15.9" fill="none"
          stroke="{status_hex(classification)}" stroke-width="3"
          stroke-dasharray="{readiness:.0f} 100"
          stroke-linecap="round"/>
      </svg>
      <div class="absolute inset-0 flex items-center justify-center">
        <span class="text-base font-bold text-gray-100">{readiness:.0f}</span>
      </div>
    </div>
    <div>
      <span class="inline-block px-2 py-0.5 rounded-full text-sm font-semibold {sc} {sb} mb-1">{classification}</span>
      <p class="text-sm font-semibold text-gray-200">{headline}</p>
    </div>
  </div>
  <p class="text-sm text-gray-400 leading-relaxed">{explanation}</p>
</div>
"""

    # Metrics grid — plain English labels for beta users
    metrics_grid = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Training Load</p>
  <div class="grid grid-cols-3 gap-2 text-center">
    <div>
      <p class="text-lg font-bold text-blue-400">{ctl:.0f}</p>
      <p class="text-xs text-gray-500">Fitness</p>
    </div>
    <div>
      <p class="text-lg font-bold text-purple-400">{atl:.0f}</p>
      <p class="text-xs text-gray-500">Fatigue</p>
    </div>
    <div>
      <p class="text-lg font-bold {tsb_color}">{tsb_sign}{tsb:.0f}</p>
      <p class="text-xs text-gray-500">Form</p>
    </div>
  </div>
  <p class="text-xs text-gray-600 text-center mt-2">Fitness builds over weeks · Fatigue is recent load · Form = Fitness minus Fatigue</p>
</div>
"""

    # Risk flag — only surface moderate/high; low is expected in training blocks
    risk_section = ""
    if risk_flag and risk_flag in ("moderate", "high"):
        risk_colors = {
            "moderate": "bg-orange-900/30 border-orange-700/50 text-orange-300",
            "high": "bg-red-900/30 border-red-700/50 text-red-300",
        }
        rc = risk_colors.get(risk_flag, "bg-gray-800 text-gray-400")
        risk_label = "Watch load — you're accumulating significant fatigue." if risk_flag == "moderate" else "High load — a recovery day should follow soon."
        risk_section = f'<div class="rounded-xl border px-4 py-3 mb-3 text-sm {rc}">⚠ {risk_label}</div>'

    # Flags
    flags_section = ""
    if flags:
        flag_items = "".join(f'<li class="text-sm text-yellow-300">⚠ {f}</li>' for f in flags)
        flags_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Flags</p>
  <ul class="space-y-1">{flag_items}</ul>
</div>
"""

    # Insights
    insights_section = ""
    if insights:
        insight_items = "".join(f'<li class="text-sm text-gray-300">• {i}</li>' for i in insights)
        insights_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Insights</p>
  <ul class="space-y-1.5">{insight_items}</ul>
</div>
"""

    body = status_section + metrics_grid + risk_section + flags_section + insights_section

    # ── Weekly Outlook card ────────────────────────────────────────────────
    weekly_outlook_section = ""
    wo = coaching_data.get("weekly_outlook") if coaching_data else None
    if wo and wo.get("focus"):
        def _session_bullets(sessions, label, item_class="text-gray-200"):
            if not sessions:
                return ""
            items = "".join(
                f'<li class="text-sm {item_class} leading-snug">• {s}</li>'
                for s in sessions
            )
            return (
                f'<div class="mb-2">'
                f'<p class="text-xs text-gray-500 mb-1">{label}</p>'
                f'<ul class="space-y-0.5">{items}</ul>'
                f'</div>'
            )

        key_html       = _session_bullets(wo.get("key_sessions", []),       "Key sessions",    "text-gray-100")
        secondary_html = _session_bullets(wo.get("secondary_sessions", []), "Supporting rides", "text-gray-300")
        endurance_html = _session_bullets(wo.get("endurance_sessions", []), "Long ride",        "text-gray-300")
        note_text      = wo.get("coaching_note", "")
        note_html      = (
            f'<div class="mt-3 pt-3 border-t border-gray-800">'
            f'<p class="text-xs text-gray-500 mb-1">Coach note</p>'
            f'<p class="text-sm text-gray-300 leading-relaxed">{note_text}</p>'
            f'</div>'
        ) if note_text else ""

        weekly_outlook_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <div class="flex items-center justify-between mb-3">
    <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Weekly Outlook</p>
    <span class="text-xs font-semibold text-cyan-400">{wo['focus']}</span>
  </div>
  {key_html}{secondary_html}{endurance_html}{note_html}
</div>
"""

    # Chat UI
    chat_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3" id="chat-panel">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Ask your coach</p>
  <div id="chat-history" class="space-y-3 mb-3 max-h-64 overflow-y-auto"></div>
  <div class="flex gap-2 flex-wrap mb-2">
    <button type="button" onclick="askQuick('How was today\'s ride?')"
      class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-3 py-1.5 rounded-full transition-colors">Today's ride</button>
    <button type="button" onclick="askQuick('What should I do tomorrow?')"
      class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-3 py-1.5 rounded-full transition-colors">Tomorrow</button>
    <button type="button" onclick="askQuick('How is my fitness trending?')"
      class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-3 py-1.5 rounded-full transition-colors">Fitness trend</button>
    <button type="button" onclick="askQuick('What are my power zones?')"
      class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-3 py-1.5 rounded-full transition-colors">Power zones</button>
  </div>
  <form id="chat-form" class="flex gap-2" onsubmit="sendChat(event)">
    <input id="chat-input" type="text" placeholder="Ask anything about your training…"
      class="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100
             placeholder-gray-500 focus:outline-none focus:border-cyan-600 min-w-0">
    <button type="submit" id="chat-send"
      class="bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-semibold px-4 py-2 rounded-lg
             transition-colors flex-shrink-0 disabled:opacity-50">
      Send
    </button>
  </form>
</div>
<script>
// Maintain conversation history for multi-turn context
const _chatHistory = [];

function askQuick(q) {{
  document.getElementById('chat-input').value = q;
  sendChat({{ preventDefault: function(){{}} }});
}}

async function sendChat(e) {{
  e.preventDefault();
  const input   = document.getElementById('chat-input');
  const q       = input.value.trim();
  if (!q) return;
  const btn     = document.getElementById('chat-send');
  const log     = document.getElementById('chat-history');

  // User bubble
  log.insertAdjacentHTML('beforeend',
    `<div class="flex justify-end">
       <div class="bg-cyan-900/40 border border-cyan-700/40 text-gray-200 text-sm rounded-xl px-3 py-2 max-w-[80%]">${{escHtml(q)}}</div>
     </div>`);
  input.value = '';
  btn.disabled = true;
  btn.textContent = '…';

  // Thinking placeholder
  const thinkId = 'think-' + Date.now();
  log.insertAdjacentHTML('beforeend',
    `<div id="${{thinkId}}" class="flex justify-start">
       <div class="bg-gray-800 text-gray-400 text-sm italic rounded-xl px-3 py-2">Thinking…</div>
     </div>`);
  log.scrollTop = log.scrollHeight;

  try {{
    const resp = await fetch('{url_for("chat")}', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ question: q, history: _chatHistory }})
    }});
    const data = await resp.json();
    document.getElementById(thinkId).remove();
    const answer = data.answer || data.error || 'No response.';

    // Store this exchange in history for next turn
    _chatHistory.push({{ role: 'user',      content: q }});
    _chatHistory.push({{ role: 'assistant', content: answer }});
    // Cap at 20 messages (10 turns) to avoid runaway context
    while (_chatHistory.length > 20) _chatHistory.shift();

    log.insertAdjacentHTML('beforeend',
      `<div class="flex justify-start">
         <div class="bg-gray-800 text-gray-200 text-sm rounded-xl px-3 py-2 max-w-[85%] leading-relaxed">${{escHtml(answer)}}</div>
       </div>`);
  }} catch(err) {{
    document.getElementById(thinkId).remove();
    log.insertAdjacentHTML('beforeend',
      `<div class="flex justify-start">
         <div class="bg-red-900/30 text-red-300 text-sm rounded-xl px-3 py-2">Connection error. Try again.</div>
       </div>`);
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'Send';
    log.scrollTop = log.scrollHeight;
  }}
}}

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
}}
</script>
"""

    body = new_user_notice + status_section + metrics_grid + risk_section + flags_section + insights_section + weekly_outlook_section + chat_section

    html = (
        _head("Coaching")
        + f"""
<div class="max-w-lg mx-auto px-4 pt-6 pb-4">
  <h1 class="text-lg font-bold text-gray-100 mb-4">Coaching</h1>
  {body}
</div>
{_nav("coach", user)}
"""
        + _foot()
    )
    return _html_response(html)


# ---------------------------------------------------------------------------
# Routes: Nutrition
# ---------------------------------------------------------------------------

@app.route("/nutrition", methods=["GET", "POST"])
@login_required
def nutrition():
    user = g.user
    today = date.today().isoformat()
    error = ""
    success = ""

    if request.method == "POST":
        log_date = request.form.get("date", today)
        try:
            calories = float(request.form.get("calories") or 0)
            carbs_g = float(request.form.get("carbs_g") or 0)
            protein_g = float(request.form.get("protein_g") or 0)
            fat_g = float(request.form.get("fat_g") or 0)
            notes = request.form.get("notes", "").strip()
            db.save_nutrition(DB_PATH, user["id"], log_date, calories, carbs_g, protein_g, fat_g, notes)
            success = "Nutrition log saved."
        except Exception as exc:
            log.error("Nutrition save error: %s", exc)
            error = "Failed to save. Please check your inputs."

    # Load today's entry for form pre-fill
    existing = db.get_nutrition(DB_PATH, user["id"], today)
    v = existing or {}

    # History (last 7 days)
    history = db.get_nutrition_history(DB_PATH, user["id"], days=7)

    calorie_target = float(user["calorie_target"] or 2500)

    form_body = f"""
{_flash_msg(error)}
{_flash_msg(success, "success")}
<div class="bg-gray-900 rounded-xl p-4 mb-4">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Log Today</p>
  <form method="post" class="space-y-3">
    <input type="hidden" name="date" value="{today}"/>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="block text-xs text-gray-400 mb-1">Calories</label>
        <input name="calories" type="number" step="1" min="0"
          value="{v.get('calories') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Carbs (g)</label>
        <input name="carbs_g" type="number" step="0.1" min="0"
          value="{v.get('carbs_g') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Protein (g)</label>
        <input name="protein_g" type="number" step="0.1" min="0"
          value="{v.get('protein_g') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Fat (g)</label>
        <input name="fat_g" type="number" step="0.1" min="0"
          value="{v.get('fat_g') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Notes</label>
      <input name="notes" type="text" maxlength="200"
        value="{v.get('notes') or ''}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Save
    </button>
  </form>
</div>
"""

    # History table
    if history:
        rows_html = ""
        for row in history:
            cal = float(row["calories"] or 0)
            balance = cal - calorie_target
            bal_color = "text-green-400" if balance >= 0 else "text-red-400"
            bal_str = f"+{balance:.0f}" if balance >= 0 else f"{balance:.0f}"
            rows_html += f"""
<tr class="border-t border-gray-800">
  <td class="py-2 pr-2 text-xs text-gray-400">{row['date']}</td>
  <td class="py-2 pr-2 text-xs text-gray-100 text-right">{cal:.0f}</td>
  <td class="py-2 pr-2 text-xs text-gray-400 text-right">{row['carbs_g'] or 0:.0f}g</td>
  <td class="py-2 pr-2 text-xs text-gray-400 text-right">{row['protein_g'] or 0:.0f}g</td>
  <td class="py-2 pr-2 text-xs text-gray-400 text-right">{row['fat_g'] or 0:.0f}g</td>
  <td class="py-2 text-xs font-medium {bal_color} text-right">{bal_str}</td>
</tr>
"""
        history_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3 overflow-x-auto">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">7-Day History</p>
  <p class="text-xs text-gray-600 mb-2">Target: {calorie_target:.0f} kcal/day</p>
  <table class="w-full min-w-0">
    <thead>
      <tr class="text-left">
        <th class="pb-1 pr-2 text-xs text-gray-600 font-medium">Date</th>
        <th class="pb-1 pr-2 text-xs text-gray-600 font-medium text-right">kcal</th>
        <th class="pb-1 pr-2 text-xs text-gray-600 font-medium text-right">Carbs</th>
        <th class="pb-1 pr-2 text-xs text-gray-600 font-medium text-right">Prot</th>
        <th class="pb-1 pr-2 text-xs text-gray-600 font-medium text-right">Fat</th>
        <th class="pb-1 text-xs text-gray-600 font-medium text-right">± target</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""
    else:
        history_section = '<p class="text-sm text-gray-500">No nutrition history yet.</p>'

    body = form_body + history_section
    return _html_response(_page("Nutrition", body, user, "food"))


# ---------------------------------------------------------------------------
# Routes: Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = g.user
    error = ""
    success = ""

    msg_param = request.args.get("msg", "")
    show_onboarding = False
    if msg_param == "strava_ok":
        success = "Strava connected!"
        show_onboarding = True
    elif msg_param == "strava_denied":
        error = "Strava connection was denied."
    elif msg_param == "strava_error":
        error = "Strava connection failed. Please try again."

    profile = db.get_training_profile(DB_PATH, user["id"])

    if request.method == "POST":
        form_type = request.form.get("form_type", "user")
        if form_type == "profile":
            # ── Training profile form ──
            try:
                goal        = request.form.get("goal", "").strip()
                goal_custom = request.form.get("goal_custom", "").strip()
                hours_t     = float(request.form.get("weekly_hours_target") or 0)
                # TSS estimated automatically: ~55 TSS/hr is typical for a club cyclist
                tss_t       = round(hours_t * 55) if hours_t > 0 else 0
                pref_day    = request.form.get("preferred_days", "").strip()
                event_name  = request.form.get("target_event_name", "").strip()
                event_date  = request.form.get("target_event_date", "").strip()
                db.save_training_profile(
                    DB_PATH, user["id"], goal, goal_custom,
                    hours_t, tss_t, pref_day, event_name, event_date
                )
                profile = db.get_training_profile(DB_PATH, user["id"])
                success = "Training plan saved."
            except Exception as exc:
                log.error("Profile save error: %s", exc)
                error = "Failed to save training plan."
        else:
            # ── User settings form ──
            try:
                ftp = float(request.form.get("ftp") or user["ftp"])
                bmr = float(request.form.get("bmr") or user["bmr"])
                calorie_target = float(request.form.get("calorie_target") or user["calorie_target"])
                name = request.form.get("name", "").strip() or user["name"]

                if ftp <= 0 or ftp > 600:
                    error = "FTP must be between 1 and 600 W."
                elif bmr <= 0 or bmr > 5000:
                    error = "BMR must be between 1 and 5000 kcal."
                elif calorie_target <= 0 or calorie_target > 10000:
                    error = "Calorie target must be between 1 and 10000 kcal."
                else:
                    db.update_user_settings(DB_PATH, user["id"], ftp=ftp, bmr=bmr,
                                            calorie_target=calorie_target, name=name)
                    success = "Settings saved."
                    user = db.get_user_by_id(DB_PATH, user["id"])
            except ValueError:
                error = "Invalid value. Please enter numbers only."
            except Exception as exc:
                log.error("Settings save error: %s", exc)
                error = "Failed to save settings."

    strava_tokens = db.get_strava_tokens(DB_PATH, user["id"])
    strava_connected = strava_tokens is not None

    if strava_connected:
        strava_section = f"""
<div class="flex items-center justify-between">
  <div>
    <p class="text-sm font-medium text-green-400">Strava connected</p>
    <p class="text-xs text-gray-500">Athlete ID: {strava_tokens['athlete_id'] or '—'}</p>
  </div>
  <span class="text-green-400 text-xl">✓</span>
</div>
"""
    else:
        strava_section = f"""
<div class="flex items-center justify-between">
  <div>
    <p class="text-sm text-gray-400">Strava not connected</p>
  </div>
  <a href="{url_for('connect_strava')}"
     class="bg-orange-500 hover:bg-orange-400 text-white text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors">
    Connect
  </a>
</div>
"""

    onboarding_banner = ""
    if show_onboarding:
        onboarding_banner = """
<div class="bg-blue-900/30 border border-blue-700/50 rounded-xl p-4 mb-3">
  <p class="text-sm font-semibold text-blue-300 mb-1">Welcome — a quick note</p>
  <p class="text-sm text-blue-200 leading-relaxed">
    This app builds your training model from rides synced via Strava.
    Because historical rides aren't imported, it takes roughly
    <strong>4–6 weeks of activity</strong> before the coaching insights become accurate.
    Keep riding and the model will improve week by week.
  </p>
</div>
"""

    body = f"""
{_flash_msg(error)}
{_flash_msg(success, "success")}
{onboarding_banner}
{_card("Strava", strava_section)}

<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Account Settings</p>
  <form method="post" class="space-y-3">
    <input type="hidden" name="form_type" value="user"/>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Name</label>
      <input name="name" type="text" value="{user['name']}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">FTP (watts)</label>
      <input name="ftp" type="number" step="1" min="50" max="600"
        value="{user['ftp']:.0f}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      <p class="text-xs text-gray-600 mt-1">Your functional threshold power — used to calculate training zones.</p>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Resting calorie burn (kcal/day)</label>
      <input name="bmr" type="number" step="1" min="500" max="5000"
        value="{user['bmr']:.0f}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      <p class="text-xs text-gray-600 mt-1">Calories your body burns at complete rest. Typically 1400–2000 for most adults.</p>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Daily calorie target (kcal)</label>
      <input name="calorie_target" type="number" step="1" min="500" max="10000"
        value="{user['calorie_target']:.0f}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Save settings
    </button>
  </form>
</div>

<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Account</p>
  <p class="text-sm text-gray-400 mb-1">{user['email']}</p>
  <p class="text-xs text-gray-600">Member since {user['created_at'][:10]}</p>
</div>
"""

    # ── Training plan card (pre-filled from profile) ──
    p = profile or {}
    _goal_opts = [
        ("Raise FTP / threshold",       "Raise FTP / threshold"),
        ("Crit racing",                 "Crit racing"),
        ("Road race prep",              "Road race prep"),
        ("10-mile TT",                  "10-mile TT"),
        ("25-mile TT",                  "25-mile TT"),
        ("50-mile TT",                  "50-mile TT"),
        ("Hill climb",                  "Hill climb"),
        ("Sportive / event prep",       "Sportive / event prep"),
        ("Gran Fondo",                  "Gran Fondo"),
        ("Lose weight",                 "Lose weight"),
        ("General fitness",             "General fitness"),
        ("Custom",                      "Custom"),
    ]
    def _sel(val, cur): return ' selected' if val == cur else ''
    goal_options = "\n".join(
        f'<option value="{v}"{_sel(v, p.get("goal", ""))}>{l}</option>'
        for v, l in _goal_opts
    )
    day_options = "\n".join(
        f'<option value="{d}"{_sel(d, p.get("preferred_days",""))}>{d}</option>'
        for d in ["", "Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    )
    profile_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Training Profile</p>
  <form method="post" class="space-y-3">
    <input type="hidden" name="form_type" value="profile"/>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Training goal</label>
      <select name="goal"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500">
        {goal_options}
      </select>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Custom goal (optional override)</label>
      <input name="goal_custom" type="text" maxlength="120"
        value="{p.get('goal_custom') or ''}"
        placeholder="e.g. Complete 100-mile sportive in June"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Weekly hours target</label>
      <input name="weekly_hours_target" type="number" step="0.5" min="0" max="40"
        value="{float(p.get('weekly_hours_target') or 0):.1f}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      <p class="text-xs text-gray-600 mt-1">Used to estimate your weekly training load.</p>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Long ride day</label>
      <select name="preferred_days"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500">
        {day_options}
      </select>
      <p class="text-xs text-gray-600 mt-1">Helps the coach place longer endurance rides during the week.</p>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Target event name</label>
      <input name="target_event_name" type="text" maxlength="120"
        value="{p.get('target_event_name') or ''}"
        placeholder="e.g. Étape du Tour"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Target event date</label>
      <input name="target_event_date" type="date"
        value="{p.get('target_event_date') or ''}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Save training plan
    </button>
  </form>
</div>
"""

    body = body + profile_section
    return _html_response(_page("Settings", body, user, "settings"))


# ---------------------------------------------------------------------------
# Routes: Admin
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin():
    user = g.user
    users = db.get_all_users(DB_PATH)
    msg = request.args.get("msg", "")
    success = ""
    if msg == "invite_created":
        invite_token = request.args.get("token", "")
        base = request.host_url.rstrip("/")
        invite_url = f"{base}/register/{invite_token}"
        success = f'Invite created: <a href="{invite_url}" class="underline text-cyan-400 break-all">{invite_url}</a>'

    # Build table rows
    rows_html = ""
    for u in users:
        _mc_row  = db.get_metrics_cache(DB_PATH, u["id"])
        mc       = dict(_mc_row) if _mc_row else {}
        coaching = db.get_coaching_cache(DB_PATH, u["id"])
        profile  = db.get_training_profile(DB_PATH, u["id"]) if hasattr(db, "get_training_profile") else None
        has_strava = db.get_strava_tokens(DB_PATH, u["id"]) is not None

        classification = coaching.get("classification", "—") if coaching else "—"
        readiness  = coaching.get("readiness_score", 0) if coaching else 0
        ctl = coaching.get("ctl", 0) if coaching else 0
        atl = coaching.get("atl", 0) if coaching else 0
        tsb = coaching.get("tsb", 0) if coaching else 0
        tsb_sign = "+" if tsb >= 0 else ""

        # Week progress from metrics_cache
        weekly_tss   = round(float(mc["weekly_tss"]), 0) if mc.get("weekly_tss") else 0
        weekly_hours = round(float(mc["weekly_moving_time_s"]) / 3600, 1) if mc.get("weekly_moving_time_s") else 0
        weekly_rides = int(mc["weekly_count"]) if mc.get("weekly_count") else 0

        # Training profile
        if profile:
            goal          = profile.get("goal_custom") or profile.get("goal") or "—"
            tss_target    = float(profile.get("weekly_tss_target") or 0)
            hours_target  = float(profile.get("weekly_hours_target") or 0)
            event_name    = profile.get("target_event_name") or ""
            event_date    = profile.get("target_event_date") or ""
            tss_pct   = f"{round(weekly_tss / tss_target * 100)}%" if tss_target else "—"
            hours_pct = f"{round(weekly_hours / hours_target * 100)}%" if hours_target else "—"
            goal_line = f'<p class="text-xs text-cyan-400 mt-0.5">🎯 {goal}</p>'
            event_line = f'<p class="text-xs text-gray-500">Event: {event_name} {event_date}</p>' if event_name else ""
            target_line = f'<p class="text-xs text-gray-500">Week: {weekly_tss:.0f} TSS ({tss_pct} of {tss_target:.0f}) · {weekly_hours}h ({hours_pct} of {hours_target:.0f}h) · {weekly_rides} rides</p>'
        else:
            goal_line   = '<p class="text-xs text-gray-600 mt-0.5 italic">No training profile</p>'
            event_line  = ""
            target_line = f'<p class="text-xs text-gray-500">Week: {weekly_tss:.0f} TSS · {weekly_hours}h · {weekly_rides} rides</p>'

        sc = status_color(classification)
        sb = status_bg(classification)

        strava_badge = (
            '<span class="text-green-400 text-xs">✓ Strava</span>'
            if has_strava
            else '<span class="text-gray-600 text-xs">No Strava</span>'
        )
        admin_badge = '<span class="text-cyan-400 text-xs ml-1">Admin</span>' if u["is_admin"] else ""
        updated = mc["updated_at"][:10] if mc.get("updated_at") else "—"

        rows_html += f"""
<div class="bg-gray-900 rounded-xl p-4 mb-2">
  <div class="flex items-start justify-between mb-1">
    <div>
      <p class="text-sm font-semibold text-gray-100">{u['name']}{admin_badge}</p>
      <p class="text-xs text-gray-500">{u['email']}</p>
      {goal_line}
      {event_line}
    </div>
    <div class="text-right flex-shrink-0 ml-2">
      <span class="inline-block px-2 py-0.5 rounded-full text-xs font-semibold {sc} {sb}">{classification}</span>
      <p class="text-xs text-gray-500 mt-0.5">Ready: {readiness:.0f}/100</p>
    </div>
  </div>
  <div class="grid grid-cols-4 gap-2 text-center my-2">
    <div>
      <p class="text-sm font-bold text-blue-400">{ctl:.0f}</p>
      <p class="text-xs text-gray-600">Fitness</p>
    </div>
    <div>
      <p class="text-sm font-bold text-purple-400">{atl:.0f}</p>
      <p class="text-xs text-gray-600">Fatigue</p>
    </div>
    <div>
      <p class="text-sm font-bold {'text-green-400' if tsb >= 0 else 'text-red-400'}">{tsb_sign}{tsb:.0f}</p>
      <p class="text-xs text-gray-600">Form</p>
    </div>
    <div>
      <p class="text-sm font-bold text-gray-300">{u['ftp']:.0f}</p>
      <p class="text-xs text-gray-600">FTP</p>
    </div>
  </div>
  {target_line}
  <div class="flex items-center justify-between mt-2">
    <div class="flex items-center gap-2">
      {strava_badge}
      <span class="text-gray-600 text-xs">sync: {updated}</span>
    </div>
    <form method="post" action="/admin/sync/{u['id']}">
      <button type="submit" class="text-xs text-cyan-500 hover:text-cyan-300 underline">
        Sync now
      </button>
    </form>
  </div>
</div>
"""

    body = f"""
{_flash_msg(success, "success") if success else ""}
<div class="mb-4">
  <form method="post" action="{url_for('admin_invite')}">
    <button type="submit"
      class="w-full bg-cyan-700 hover:bg-cyan-600 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors">
      + Create Invite Link
    </button>
  </form>
</div>
<p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Members ({len(users)})</p>
{rows_html}
"""
    return _html_response(_page("Admin", body, user, "admin"))


@app.route("/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    user = g.user
    try:
        token = db.create_invite(DB_PATH, user["id"])
        return redirect(url_for("admin") + f"?msg=invite_created&token={token}")
    except Exception as exc:
        log.error("Failed to create invite: %s", exc)
        return redirect(url_for("admin"))


@app.route("/admin/sync/<int:uid>", methods=["POST"])
@admin_required
def admin_sync(uid: int):
    """Trigger an immediate sync for a single user."""
    import threading

    def _do_sync(db_path, user_id, client_id, client_secret):
        try:
            token = strava.get_valid_token(db_path, user_id, client_id, client_secret)
            activities = strava.fetch_activities(token)
            target_user = db.get_user_by_id(db_path, user_id)
            ftp = target_user["ftp"] if target_user else 200
            values = metrics_mod.compute_metrics(activities, ftp)
            db.save_metrics_cache(db_path, user_id, values)
            state = coach_mod.TrainingState.from_metrics(values)
            result = coach_mod.evaluate(state)
            result_dict = result.to_dict()

            # Weekly outlook — deterministic, no LLM
            profile       = db.get_training_profile(db_path, user_id)
            goal          = (profile.get("goal_custom") or profile.get("goal") or "") if profile else ""
            long_ride_day = profile.get("preferred_days", "") if profile else ""
            result_dict["weekly_outlook"] = coach_mod.generate_weekly_outlook(
                state, goal, long_ride_day
            )

            db.save_coaching_cache(db_path, user_id, result_dict)
            log.info("Manual sync complete for user %s", user_id)
        except Exception as exc:
            log.error("Manual sync failed for user %s: %s", user_id, exc)

    t = threading.Thread(target=_do_sync, args=(DB_PATH, uid, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET), daemon=True)
    t.start()

    return redirect(url_for("admin") + "?msg=sync_started")


# ---------------------------------------------------------------------------
# Chat (AI coach via mcp-coach)
# ---------------------------------------------------------------------------

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    user = g.user
    body = request.get_json(force=True, silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return {"error": "empty question"}, 400
    # history: list of {"role": "user"|"assistant", "content": "..."}
    history = body.get("history", [])
    if not isinstance(history, list):
        history = []
    try:
        payload = json.dumps({
            "user_id": user["id"],
            "question": question,
            "history": history,
        }).encode()
        req = urllib.request.Request(
            f"{MCP_COACH_URL}/ask",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return {"answer": data.get("answer", ""), "source": data.get("source", "")}, 200
    except urllib.error.URLError as exc:
        log.error("mcp-coach unreachable: %s", exc)
        return {"error": "AI coach unavailable right now."}, 503
    except Exception as exc:
        log.error("chat error: %s", exc)
        return {"error": str(exc)}, 500


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    try:
        conn = db.get_conn(DB_PATH)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok"}, 200
    except Exception as exc:
        log.error("Health check failed: %s", exc)
        return {"status": "error", "detail": str(exc)}, 500


# ---------------------------------------------------------------------------
# Poller startup
# ---------------------------------------------------------------------------

_poller_started = False


def start_poller():
    """Start the background poller. Safe to call multiple times (idempotent)."""
    global _poller_started
    if _poller_started:
        return
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        log.warning("Strava credentials not set — poller will not start.")
        return
    _poller_started = True
    p = Poller(DB_PATH, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, interval=POLL_INTERVAL)
    p.start()
    log.info("Poller started (interval=%ds)", POLL_INTERVAL)


# Start poller for gunicorn (import-time, runs in worker process)
start_poller()


if __name__ == "__main__":
    start_poller()
    app.run(host="0.0.0.0", port=9206, debug=False)
