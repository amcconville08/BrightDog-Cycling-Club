"""
app.py - Cycling Club Flask web application.
All HTML is inline. No templates directory.
Dark theme, mobile-first, Tailwind CDN.
"""
import os
import json
import logging
import functools
from datetime import datetime, timezone, date, timedelta

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
import reconciliation as recon_mod
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
    if not m:
        return "0.0 km"
    return f"{m / 1000:.1f} km"


def fmt_time(s: float) -> str:
    if not s:
        return "0h 00m"
    s = int(s)
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m:02d}m"


def fmt_date(ts: float) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%-d %b")
    except Exception:
        return "—"


def fmt_elev(m: float) -> str:
    if not m:
        return "0 m"
    return f"{int(m):,} m".replace(",", " ")


def status_color(classification: str) -> str:
    mapping = {
        "Fresh":              "text-green-400",
        "Ready":              "text-cyan-400",
        "Slight Fatigue":     "text-sky-300",
        "Productive Fatigue": "text-yellow-400",
        "Heavy Load":         "text-orange-400",
        "Very Fatigued":      "text-red-400",
        "Deep Fatigue":       "text-red-500",
        # legacy keys
        "Balanced":           "text-cyan-400",
        "Fatigued":           "text-yellow-400",
        "Overreaching":       "text-red-500",
    }
    return mapping.get(classification, "text-gray-400")


def status_bg(classification: str) -> str:
    mapping = {
        "Fresh":              "bg-green-900/30",
        "Ready":              "bg-cyan-900/30",
        "Slight Fatigue":     "bg-sky-900/30",
        "Productive Fatigue": "bg-yellow-900/30",
        "Heavy Load":         "bg-orange-900/30",
        "Very Fatigued":      "bg-red-900/30",
        "Deep Fatigue":       "bg-red-900/50",
        # legacy keys
        "Balanced":           "bg-cyan-900/30",
        "Fatigued":           "bg-yellow-900/30",
        "Overreaching":       "bg-red-900/50",
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
    if not user:
        return ""

    def _tab(href, label, icon, key):
        active = "text-cyan-400 border-t-2 border-cyan-400" if current == key else "text-gray-500"
        return (f'<a href="{href}" class="flex flex-col items-center flex-1 py-2 {active} '
                f'text-xs font-medium"><span class="text-xl mb-0.5">{icon}</span>{label}</a>')

    admin_tab = ""
    if user["is_admin"]:
        admin_tab = _tab(url_for("admin"), "Admin", "👑", "admin")

    return f"""
<nav class="fixed bottom-0 left-0 right-0 bg-gray-900 border-t border-gray-800 flex bottom-safe z-50">
  {_tab(url_for("dashboard"), "Home", "🏠", "home")}
  {_tab(url_for("coaching"), "Coach", "🎯", "coach")}
  {_tab(url_for("plan"), "Plan", "📅", "plan")}
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


def _flash_msg(msg: str, kind: str = "error") -> str:
    colour = ("bg-red-900/50 text-red-300 border border-red-700"
              if kind == "error"
              else "bg-green-900/50 text-green-300 border border-green-700")
    return f'<div class="rounded-lg px-4 py-3 mb-4 text-sm {colour}">{msg}</div>' if msg else ""


def _input(name, label, type_="text", value="", extra=""):
    return f"""
<div>
  <label class="block text-xs text-gray-400 mb-1">{label}</label>
  <input name="{name}" type="{type_}" value="{value}" {extra}
    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
</div>"""


def _select(name, label, options, selected=""):
    opts = "".join(
        f'<option value="{v}" {"selected" if v == selected else ""}>{l}</option>'
        for v, l in options
    )
    return f"""
<div>
  <label class="block text-xs text-gray-400 mb-1">{label}</label>
  <select name="{name}"
    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500">
    {opts}
  </select>
</div>"""


# Workout type display helpers
_WORKOUT_TYPE_LABELS = {
    "endurance":  ("Endurance",  "text-blue-400",   "🚴"),
    "recovery":   ("Recovery",   "text-green-400",  "💚"),
    "tempo":      ("Tempo",      "text-yellow-400", "⚡"),
    "threshold":  ("Threshold",  "text-orange-400", "🔥"),
    "vo2":        ("VO2 Max",    "text-red-400",    "🔴"),
    "race":       ("Race",       "text-purple-400", "🏆"),
    "strength":   ("Strength",   "text-cyan-400",   "💪"),
    "rest":       ("Rest Day",   "text-gray-400",   "😴"),
}

def _workout_type_badge(type_: str) -> str:
    label, color, icon = _WORKOUT_TYPE_LABELS.get(type_, (type_.title(), "text-gray-400", "📋"))
    return f'<span class="{color} text-xs font-medium">{icon} {label}</span>'


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
<p class="text-center text-sm text-gray-500 mt-6">
  Not a member yet? Contact your club admin for an invite link.
</p>
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

    if not invite:
        return _html_response(_page("Invalid Invite", "<p class='text-red-400'>This invite link is invalid.</p>"))
    if invite["used_by_id"]:
        return _html_response(_page("Invalid Invite", "<p class='text-red-400'>This invite has already been used.</p>"))

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
        STRAVA_CLIENT_ID, STRAVA_REDIRECT_URI,
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
        return redirect(url_for("settings") + "?msg=strava_denied")
    if not code:
        return redirect(url_for("settings") + "?msg=strava_error")

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
            DB_PATH, uid,
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=token_data["expires_at"],
            athlete_id=athlete.get("id"),
        )
        log.info("Strava connected for user %s (athlete %s)", uid, athlete.get("id"))
    except Exception as exc:
        log.error("Strava token exchange failed: %s", exc)
        return redirect(url_for("settings") + "?msg=strava_error")

    if not session.get("user_id"):
        session["user_id"] = uid

    return redirect(url_for("settings") + "?msg=strava_ok")


@app.route("/disconnect-strava", methods=["POST"])
@login_required
def disconnect_strava():
    db.delete_strava_tokens(DB_PATH, g.user["id"])
    log.info("Strava disconnected for user %s", g.user["id"])
    return redirect(url_for("settings") + "?msg=strava_disconnected")


# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    user = g.user
    mc_row = db.get_metrics_cache(DB_PATH, user["id"])
    mc = dict(mc_row) if mc_row else None
    coaching = db.get_coaching_cache(DB_PATH, user["id"])
    has_strava = db.get_strava_tokens(DB_PATH, user["id"]) is not None
    today_str = date.today().isoformat()
    today_workout = db.get_today_workout(DB_PATH, user["id"], today_str)
    profile = db.get_training_profile(DB_PATH, user["id"])

    classification = coaching.get("classification", "—")
    readiness = coaching.get("readiness_score", 0)
    recommendation = coaching.get("recommendation", "")
    sc = status_color(classification)
    sb = status_bg(classification)

    # ------------------------------------------------------------------
    # CARD 1 — Status hero (taps through to Coach screen)
    # ------------------------------------------------------------------
    if not has_strava:
        status_body = '<p class="text-sm text-gray-400">Connect Strava to get started.</p>'
        status_sub = ""
    elif not coaching:
        status_body = '<p class="text-sm text-gray-400">Syncing your data — check back shortly.</p>'
        status_sub = ""
    else:
        status_body = f'<span class="inline-block px-2.5 py-0.5 rounded-full text-sm font-semibold {sc} {sb}">{classification}</span>'
        status_sub = f'<p class="text-base text-gray-200 mt-2 leading-snug font-medium">{recommendation}</p>'

    ctl = coaching.get("ctl", 0)

    status_card = f"""
<a href="{url_for('coaching')}" class="block">
<div class="bg-gray-900 rounded-xl p-5 mb-3 flex items-center gap-4 active:opacity-80">
  <div class="relative w-16 h-16 flex-shrink-0">
    <svg class="w-16 h-16 -rotate-90" viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="15.9" fill="none" stroke="#1f2937" stroke-width="3"/>
      <circle cx="18" cy="18" r="15.9" fill="none"
        stroke="{status_hex(classification)}" stroke-width="3"
        stroke-dasharray="{readiness:.0f} 100"
        stroke-linecap="round"/>
    </svg>
    <div class="absolute inset-0 flex flex-col items-center justify-center">
      <span class="text-sm font-bold text-gray-100 leading-none">{ctl:.0f}</span>
      <span class="text-gray-500 leading-none" style="font-size:9px">CTL</span>
    </div>
  </div>
  <div class="flex-1 min-w-0">
    <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Readiness</p>
    {status_body}
    {status_sub}
  </div>
  <span class="text-gray-600 text-lg flex-shrink-0">›</span>
</div>
</a>
"""

    # ------------------------------------------------------------------
    # CARD 2 — Today's Ride (taps through to Coach screen)
    # ------------------------------------------------------------------
    ride_style = coaching.get("ride_style", "")
    ride_hint = coaching.get("ride_hint", "")
    ride_rationale = coaching.get("ride_style_rationale", "")
    suggested_dur = coaching.get("suggested_duration_minutes", 0)
    suggested_tss = coaching.get("suggested_tss", 0)

    ride_card = ""
    if ride_style and has_strava:
        style_icon = recon_mod.STYLE_ICONS.get(ride_style, "🚴")
        dur_str = f"{suggested_dur} min" if suggested_dur else ""
        tss_str = f"~{suggested_tss} TSS" if suggested_tss else ""
        meta_str = " · ".join(filter(None, [dur_str, tss_str]))

        # Note if a workout is planned for today
        plan_note = ""
        if today_workout:
            pw = dict(today_workout)
            plan_note = f'<p class="text-xs text-gray-600 mt-2">Planned: {pw["title"]}</p>'

        ride_card = f"""
<a href="{url_for('coaching')}" class="block">
<div class="bg-gray-900 rounded-xl p-4 mb-3 active:opacity-80">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Today's Ride</p>
  <div class="flex items-start justify-between">
    <div class="flex-1 min-w-0">
      <p class="text-xl font-bold text-gray-100">{style_icon} {ride_style}</p>
      {f'<p class="text-xs text-gray-500 mt-1">{meta_str}</p>' if meta_str else ''}
      {f'<p class="text-sm text-gray-400 mt-2 leading-relaxed italic">{ride_hint}</p>' if ride_hint else ''}
      {plan_note}
    </div>
    <span class="text-gray-600 text-lg ml-3 flex-shrink-0">›</span>
  </div>
</div>
</a>
"""
    elif has_strava and not coaching:
        ride_card = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Today's Ride</p>
  <p class="text-sm text-gray-500">Syncing — back shortly.</p>
</div>
"""

    # ------------------------------------------------------------------
    # CARD 3 — This Week (with inline <details> expand for more)
    # ------------------------------------------------------------------
    if mc:
        weekly_tss = float(mc["weekly_tss"] or 0) if "weekly_tss" in mc.keys() else 0.0
        week_longest = float(mc["weekly_longest_distance_m"] or 0) if "weekly_longest_distance_m" in mc.keys() else 0.0
        wc = int(mc["weekly_count"] or 0)
        wt = fmt_time(mc["weekly_moving_time_s"])

        # TSS progress bar
        weekly_tss_target = float(profile.get("weekly_tss_target") or 0) if profile else 0.0
        tss_bar = ""
        if weekly_tss_target > 0:
            pct = min(100, int(weekly_tss / weekly_tss_target * 100))
            bar_color = "bg-cyan-500" if pct < 100 else "bg-green-500"
            if pct >= 110:
                bar_color = "bg-yellow-500"
            tss_bar = f"""
<div class="mt-3">
  <div class="flex justify-between text-xs text-gray-500 mb-1">
    <span>{weekly_tss:.0f} TSS</span><span>{pct}% of target</span>
  </div>
  <div class="w-full bg-gray-800 rounded-full h-1.5">
    <div class="{bar_color} h-1.5 rounded-full" style="width:{pct}%"></div>
  </div>
</div>"""

        week_summary = f"{wc} ride{'s' if wc != 1 else ''} · {wt}"
        if not weekly_tss_target and weekly_tss > 0:
            week_summary += f" · {weekly_tss:.0f} TSS"

        # Monthly stats live in the details expand
        month_detail = ""
        if mc:
            month_detail = f"""
<div class="pt-3 mt-3 border-t border-gray-800">
  <p class="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-2">This Month</p>
  <div class="grid grid-cols-3 gap-2 text-center">
    <div>
      <p class="text-sm font-bold text-gray-300">{fmt_dist(mc['monthly_distance_m'])}</p>
      <p class="text-xs text-gray-600">Distance</p>
    </div>
    <div>
      <p class="text-sm font-bold text-gray-300">{mc['monthly_count']}</p>
      <p class="text-xs text-gray-600">Rides</p>
    </div>
    <div>
      <p class="text-sm font-bold text-gray-300">{fmt_elev(mc['weekly_elevation_m'])}</p>
      <p class="text-xs text-gray-600">Week elev</p>
    </div>
  </div>
</div>"""

        week_card = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <details>
    <summary class="flex items-center justify-between cursor-pointer list-none">
      <div>
        <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">This Week</p>
        <p class="text-base font-bold text-gray-100">{week_summary}</p>
      </div>
      <span class="text-gray-600 text-lg">›</span>
    </summary>
    <div class="mt-3 grid grid-cols-3 gap-2 text-center">
      <div>
        <p class="text-sm font-bold text-gray-200">{fmt_dist(mc['weekly_distance_m'])}</p>
        <p class="text-xs text-gray-500">Distance</p>
      </div>
      <div>
        <p class="text-sm font-bold text-yellow-400">{weekly_tss:.0f}</p>
        <p class="text-xs text-gray-500">TSS</p>
      </div>
      <div>
        <p class="text-sm font-bold text-gray-200">{fmt_dist(week_longest)}</p>
        <p class="text-xs text-gray-500">Longest</p>
      </div>
    </div>
    {tss_bar}
    {month_detail}
  </details>
</div>
"""
    else:
        week_card = _card("This Week", '<p class="text-sm text-gray-500">No data yet.</p>')

    # ------------------------------------------------------------------
    # CARD 4 — Last Ride (with inline <details> expand)
    # ------------------------------------------------------------------
    if mc and mc["last_start_ts"]:
        last_dist = fmt_dist(mc["last_distance_m"])
        last_time = fmt_time(mc["last_moving_time_s"])
        last_date = fmt_date(mc["last_start_ts"])
        last_elev = fmt_elev(mc["last_elevation_m"])
        last_watts = f'{mc["last_avg_watts"]:.0f} W' if mc["last_avg_watts"] else "—"

        last_ride_card = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <details>
    <summary class="flex items-center justify-between cursor-pointer list-none">
      <div>
        <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Last Ride</p>
        <p class="text-base font-bold text-gray-100">{last_dist}</p>
        <p class="text-xs text-gray-500 mt-0.5">{last_date} · {last_time}</p>
      </div>
      <span class="text-gray-600 text-lg">›</span>
    </summary>
    <div class="mt-3 grid grid-cols-3 gap-2 text-center border-t border-gray-800 pt-3">
      <div>
        <p class="text-sm font-bold text-gray-200">{last_time}</p>
        <p class="text-xs text-gray-500">Duration</p>
      </div>
      <div>
        <p class="text-sm font-bold text-gray-200">{last_elev}</p>
        <p class="text-xs text-gray-500">Elevation</p>
      </div>
      <div>
        <p class="text-sm font-bold text-gray-200">{last_watts}</p>
        <p class="text-xs text-gray-500">Avg power</p>
      </div>
    </div>
  </details>
</div>
"""
    else:
        last_ride_card = _card("Last Ride", '<p class="text-sm text-gray-500">No ride data yet.</p>')

    # ------------------------------------------------------------------
    # CARD 5 — Goal
    # ------------------------------------------------------------------
    goal = profile.get("goal", "") if profile else ""
    goal_custom = profile.get("goal_custom", "") if profile else ""
    effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal
    target_event_date = profile.get("target_event_date", "") if profile else ""
    target_event_name = profile.get("target_event_name", "") if profile else ""

    event_line = ""
    if effective_goal and target_event_date:
        try:
            days_left = (date.fromisoformat(target_event_date) - date.today()).days
            if days_left >= 0:
                event_label = target_event_name or "Event"
                event_line = f'<p class="text-xs text-gray-500 mt-1">{event_label} · {days_left} days away</p>'
        except ValueError:
            pass

    if effective_goal:
        goal_card = f"""
<a href="{url_for('training_profile')}" class="block">
<div class="bg-gray-900 rounded-xl p-4 mb-3 flex items-center justify-between active:opacity-80">
  <div>
    <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Goal</p>
    <p class="text-sm font-medium text-gray-200">{effective_goal}</p>
    {event_line}
  </div>
  <span class="text-gray-600 text-lg">›</span>
</div>
</a>
"""
    else:
        goal_card = f"""
<a href="{url_for('training_profile')}" class="block">
<div class="bg-cyan-900/20 border border-cyan-700/40 rounded-xl p-4 mb-3 flex items-center justify-between active:opacity-80">
  <div>
    <p class="text-xs font-semibold text-cyan-600 uppercase tracking-wider mb-1">Goal</p>
    <p class="text-sm text-cyan-300">Set a training goal for personalised coaching →</p>
  </div>
</div>
</a>
"""

    # ------------------------------------------------------------------
    # Strava connect prompt (only when not connected)
    # ------------------------------------------------------------------
    strava_prompt = ""
    if not has_strava:
        strava_prompt = f"""
<a href="{url_for('connect_strava')}" class="block">
<div class="bg-orange-900/20 border border-orange-700/50 rounded-xl p-4 mb-3 flex items-center justify-between active:opacity-80">
  <div>
    <p class="text-sm font-semibold text-orange-300">Connect Strava</p>
    <p class="text-xs text-gray-500 mt-0.5">Link your account to see fitness metrics</p>
  </div>
  <span class="text-orange-400 text-lg">›</span>
</div>
</a>
"""

    updated_at = ""
    if mc and mc.get("updated_at"):
        updated_at = f'<p class="text-xs text-gray-700 text-center mt-2">Synced {mc["updated_at"][:16]} UTC</p>'

    body = (strava_prompt + status_card + ride_card + week_card +
            last_ride_card + goal_card + updated_at)

    html = (
        _head(f"Home — {user['name']}")
        + f"""
<div class="max-w-lg mx-auto px-4 pt-6 pb-4">
  <div class="flex items-center justify-between mb-4">
    <div>
      <h1 class="text-lg font-bold text-gray-100">Hi, {user['name'].split()[0]} 👋</h1>
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
    today_str = date.today().isoformat()
    today_workout = db.get_today_workout(DB_PATH, user["id"], today_str)
    profile = db.get_training_profile(DB_PATH, user["id"])

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
            no_data = '<p class="text-sm text-gray-500">Syncing data — check back shortly.</p>'
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

    sc = status_color(classification)
    sb = status_bg(classification)
    tsb_sign = "+" if tsb >= 0 else ""
    tsb_color = "text-green-400" if tsb >= 0 else "text-red-400"

    # Status header
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

    # Metrics grid
    metrics_grid = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Metrics</p>
  <div class="grid grid-cols-5 gap-1 text-center">
    <div>
      <p class="text-base font-bold text-blue-400">{ctl:.0f}</p>
      <p class="text-xs text-gray-500">CTL</p>
    </div>
    <div>
      <p class="text-base font-bold text-purple-400">{atl:.0f}</p>
      <p class="text-xs text-gray-500">ATL</p>
    </div>
    <div>
      <p class="text-base font-bold {tsb_color}">{tsb_sign}{tsb:.0f}</p>
      <p class="text-xs text-gray-500">TSB</p>
    </div>
    <div>
      <p class="text-base font-bold text-gray-300">{ratio:.2f}</p>
      <p class="text-xs text-gray-500">Ratio</p>
    </div>
    <div>
      <p class="text-base font-bold text-yellow-400">{tss_today:.0f}</p>
      <p class="text-xs text-gray-500">TSS</p>
    </div>
  </div>
</div>
"""

    # Today's planned workout
    workout_section = ""
    if today_workout:
        pw = dict(today_workout)
        dur_str = f"{pw['target_duration_min']}min" if pw.get("target_duration_min") else ""
        tss_str = f"target {pw['target_tss']:.0f} TSS" if pw.get("target_tss") else ""
        details = " · ".join(filter(None, [dur_str, tss_str]))
        workout_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Today's Planned Workout</p>
  <div class="flex items-start justify-between">
    <div>
      <p class="text-sm font-semibold text-gray-100">{pw['title']}</p>
      <div class="mt-1">{_workout_type_badge(pw['type'])}</div>
      {f'<p class="text-xs text-gray-500 mt-1">{details}</p>' if details else ''}
      {f'<p class="text-xs text-gray-400 mt-1 italic">{pw["notes"]}</p>' if pw.get("notes") else ''}
    </div>
    <a href="{url_for('plan')}" class="text-xs text-cyan-500 hover:text-cyan-300">Plan →</a>
  </div>
</div>
"""

    # Training goal context
    goal_section = ""
    goal = profile.get("goal", "")
    goal_custom = profile.get("goal_custom", "")
    effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal
    if effective_goal:
        target_event = profile.get("target_event_name", "")
        event_html = f'<p class="text-xs text-gray-500 mt-1">Event: {target_event}</p>' if target_event else ""
        goal_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <div class="flex items-start justify-between">
    <div>
      <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Training Goal</p>
      <p class="text-sm text-gray-200">{effective_goal}</p>
      {event_html}
    </div>
    <a href="{url_for('training_profile')}" class="text-xs text-cyan-500 hover:text-cyan-300">Edit</a>
  </div>
</div>
"""
    else:
        goal_section = f"""
<div class="bg-cyan-900/20 border border-cyan-700/40 rounded-xl p-4 mb-3">
  <p class="text-xs text-cyan-400 mb-1">No training goal set</p>
  <p class="text-xs text-gray-400 mb-2">Set a goal to get more specific coaching insights.</p>
  <a href="{url_for('training_profile')}"
     class="text-xs text-cyan-400 underline hover:text-cyan-300">Set training goal →</a>
</div>
"""

    # Risk flag
    risk_section = ""
    if risk_flag:
        risk_colors = {
            "low":      "bg-yellow-900/30 border-yellow-700/50 text-yellow-300",
            "moderate": "bg-orange-900/30 border-orange-700/50 text-orange-300",
            "high":     "bg-red-900/30 border-red-700/50 text-red-300",
        }
        rc = risk_colors.get(risk_flag, "bg-gray-800 text-gray-400")
        risk_section = f'<div class="rounded-xl border px-4 py-3 mb-3 text-sm font-medium {rc}">Risk: {risk_flag.title()}</div>'

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
        insight_items = "".join(f'<li class="text-sm text-gray-300 leading-relaxed">• {i}</li>' for i in insights)
        insights_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Insights</p>
  <ul class="space-y-2">{insight_items}</ul>
</div>
"""

    # Ride style suggestion
    ride_style = coaching_data.get("ride_style", "")
    ride_rationale = coaching_data.get("ride_style_rationale", "")
    suggested_dur = coaching_data.get("suggested_duration_minutes", 0)
    suggested_tss = coaching_data.get("suggested_tss", 0)
    recon_state = coaching_data.get("recon_state")

    ride_style_section = ""
    if ride_style:
        style_icon = recon_mod.STYLE_ICONS.get(ride_style, "🚴")
        dur_str = f"{suggested_dur} min" if suggested_dur else ""
        tss_str = f"~{suggested_tss} TSS" if suggested_tss else ""
        details_str = " · ".join(filter(None, [dur_str, tss_str]))
        ride_style_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Today's Ride Suggestion</p>
  <div class="flex items-center justify-between mb-2">
    <p class="text-base font-bold text-gray-100">{style_icon} {ride_style}</p>
    {f'<p class="text-xs text-gray-500">{details_str}</p>' if details_str else ''}
  </div>
  {f'<p class="text-sm text-gray-400 leading-relaxed">{ride_rationale}</p>' if ride_rationale else ''}
</div>
"""

    # Yesterday vs Plan
    yesterday_recon_section = ""
    if recon_state:
        label, color, icon = recon_mod.RECON_LABELS.get(
            recon_state, (recon_state, "text-gray-400", "?"))
        rpt = coaching_data.get("recon_planned_title", "")
        ran = coaching_data.get("recon_actual_name", "")
        rpt_tss = coaching_data.get("recon_planned_tss", 0)
        rat_tss = coaching_data.get("recon_actual_tss", 0)
        tss_line = ""
        if recon_state not in (recon_mod.UNPLANNED_RIDE, recon_mod.PLAN_SKIPPED) and rpt_tss:
            tss_line = f'<p class="text-xs text-gray-500 mt-1">{rat_tss:.0f} TSS actual vs {rpt_tss:.0f} TSS planned</p>'
        elif recon_state == recon_mod.UNPLANNED_RIDE and rat_tss:
            tss_line = f'<p class="text-xs text-gray-500 mt-1">{rat_tss:.0f} TSS</p>'
        name_line = ran or rpt
        yesterday_recon_section = f"""
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Yesterday vs Plan</p>
  <div class="flex items-center gap-2">
    <span class="{color} text-sm font-semibold">{icon} {label}</span>
    {f'<span class="text-xs text-gray-500">· {name_line}</span>' if name_line else ''}
  </div>
  {tss_line}
</div>
"""

    body = (status_section + ride_style_section + metrics_grid +
            yesterday_recon_section + workout_section + goal_section +
            risk_section + flags_section + insights_section)

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
# Routes: Plan
# ---------------------------------------------------------------------------

_WORKOUT_TYPES = [
    ("endurance",  "Endurance"),
    ("recovery",   "Recovery"),
    ("tempo",      "Tempo"),
    ("threshold",  "Threshold"),
    ("vo2",        "VO2 Max"),
    ("race",       "Race"),
    ("strength",   "Strength"),
    ("rest",       "Rest Day"),
]

@app.route("/plan", methods=["GET", "POST"])
@login_required
def plan():
    user = g.user
    error = ""
    success = ""
    today_str = date.today().isoformat()

    if request.method == "POST":
        try:
            w_date = request.form.get("date", today_str)
            title = request.form.get("title", "").strip()
            type_ = request.form.get("type", "endurance")
            target_duration_min = int(request.form.get("target_duration_min") or 0)
            target_tss = float(request.form.get("target_tss") or 0)
            notes = request.form.get("notes", "").strip()
            target_power_zone = request.form.get("target_power_zone", "").strip()
            target_hr_low = int(request.form.get("target_hr_low") or 0)
            target_hr_high = int(request.form.get("target_hr_high") or 0)

            if not title:
                error = "Please enter a session title."
            elif not w_date:
                error = "Please select a date."
            else:
                db.save_planned_workout(
                    DB_PATH, user["id"], w_date, title, type_,
                    target_duration_min, target_tss, notes,
                    target_power_zone, target_hr_low, target_hr_high
                )
                success = "Workout added."
        except Exception as exc:
            log.error("Plan save error: %s", exc)
            error = "Failed to save workout."

    # Load upcoming 4 weeks + past 3 days
    from_date = (date.today() - timedelta(days=3)).isoformat()
    to_date = (date.today() + timedelta(days=28)).isoformat()
    workouts = db.get_planned_workouts(DB_PATH, user["id"], from_date=from_date, to_date=to_date)

    # Render workout list
    workout_rows = ""
    if workouts:
        for w in workouts:
            w = dict(w)
            is_today = w["date"] == today_str
            is_past = w["date"] < today_str
            border = "border-cyan-700/60" if is_today else ("border-gray-700/40" if not is_past else "border-gray-800/40")
            opacity = "opacity-50" if is_past else ""
            today_badge = '<span class="text-xs bg-cyan-800 text-cyan-300 px-1.5 py-0.5 rounded ml-2">Today</span>' if is_today else ""
            dur_str = f"{w['target_duration_min']}min" if w.get("target_duration_min") else ""
            tss_str = f"{w['target_tss']:.0f} TSS" if w.get("target_tss") else ""
            details = " · ".join(filter(None, [dur_str, tss_str]))

            # Format date nicely
            try:
                d = date.fromisoformat(w["date"])
                date_label = d.strftime("%-d %b")
                day_label = d.strftime("%a")
            except Exception:
                date_label = w["date"]
                day_label = ""

            workout_rows += f"""
<div class="bg-gray-900 border {border} rounded-xl p-4 mb-2 {opacity}">
  <div class="flex items-start justify-between">
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2 mb-1">
        <span class="text-xs text-gray-500 font-medium">{day_label} {date_label}</span>
        {today_badge}
      </div>
      <p class="text-sm font-semibold text-gray-100">{w['title']}</p>
      <div class="mt-1 flex items-center gap-3">
        {_workout_type_badge(w['type'])}
        {f'<span class="text-xs text-gray-500">{details}</span>' if details else ''}
      </div>
      {f'<p class="text-xs text-gray-500 mt-1 italic">{w["notes"]}</p>' if w.get("notes") else ''}
    </div>
    <form method="post" action="{url_for('plan_delete', wid=w['id'])}" class="ml-3 flex-shrink-0">
      <button type="submit" class="text-gray-600 hover:text-red-400 text-lg leading-none" title="Delete">×</button>
    </form>
  </div>
</div>
"""
    else:
        workout_rows = '<p class="text-sm text-gray-500 mb-4">No workouts planned yet.</p>'

    # Add workout form
    add_form = f"""
{_flash_msg(error)}
{_flash_msg(success, "success")}
<div class="bg-gray-900 rounded-xl p-4 mb-4">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Add Workout</p>
  <form method="post" class="space-y-3">
    <div class="grid grid-cols-2 gap-3">
      {_input("date", "Date", "date", today_str)}
      {_select("type", "Type", _WORKOUT_TYPES, "endurance")}
    </div>
    {_input("title", "Session title", "text", "", 'placeholder="e.g. 2h endurance ride"')}
    <div class="grid grid-cols-2 gap-3">
      {_input("target_duration_min", "Duration (min)", "number", "", "min='0' max='600'")}
      {_input("target_tss", "Target TSS", "number", "", "min='0' max='500' step='1'")}
    </div>
    {_input("target_power_zone", "Power zone (optional)", "text", "", 'placeholder="e.g. Z2, Sweet Spot"')}
    <div class="grid grid-cols-2 gap-3">
      {_input("target_hr_low", "HR low (bpm)", "number", "", "min='0'")}
      {_input("target_hr_high", "HR high (bpm)", "number", "", "min='0'")}
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Notes</label>
      <textarea name="notes" rows="2" maxlength="500"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"
        placeholder="Optional session notes..."></textarea>
    </div>
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Add to plan
    </button>
  </form>
</div>
"""

    body = add_form + f'<p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Upcoming</p>' + workout_rows
    return _html_response(_page("Plan", body, user, "plan"))


@app.route("/plan/delete/<int:wid>", methods=["POST"])
@login_required
def plan_delete(wid: int):
    db.delete_planned_workout(DB_PATH, wid, g.user["id"])
    return redirect(url_for("plan"))


# ---------------------------------------------------------------------------
# Routes: Training Profile
# ---------------------------------------------------------------------------

_GOAL_OPTIONS = [
    ("", "— Select a goal —"),
    ("Build aerobic base", "Build aerobic base"),
    ("Improve endurance for longer rides", "Improve endurance for longer rides"),
    ("Raise FTP / threshold", "Raise FTP / threshold"),
    ("Prepare for an event", "Prepare for an event"),
    ("Weight loss while maintaining performance", "Weight loss while maintaining performance"),
    ("General consistency / fitness", "General consistency / fitness"),
    ("Custom", "Custom (describe below)"),
]

_DISCIPLINE_OPTIONS = [
    ("road",   "Road"),
    ("gravel", "Gravel"),
    ("mtb",    "Mountain Bike"),
    ("mixed",  "Mixed"),
]

_DAY_OPTIONS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@app.route("/training-profile", methods=["GET", "POST"])
@login_required
def training_profile():
    user = g.user
    error = ""
    success = ""
    p = db.get_training_profile(DB_PATH, user["id"])

    if request.method == "POST":
        try:
            goal = request.form.get("goal", "")
            goal_custom = request.form.get("goal_custom", "").strip()
            preferred_days = ",".join(request.form.getlist("preferred_days"))
            weekly_hours_target = float(request.form.get("weekly_hours_target") or 0)
            discipline = request.form.get("discipline", "road")
            target_event_date = request.form.get("target_event_date", "").strip()
            target_event_name = request.form.get("target_event_name", "").strip()
            weekly_rides_target = int(request.form.get("weekly_rides_target") or 0)
            weekly_tss_target = float(request.form.get("weekly_tss_target") or 0)

            db.save_training_profile(
                DB_PATH, user["id"], goal=goal, goal_custom=goal_custom,
                preferred_days=preferred_days, weekly_hours_target=weekly_hours_target,
                discipline=discipline, target_event_date=target_event_date,
                target_event_name=target_event_name, weekly_rides_target=weekly_rides_target,
                weekly_tss_target=weekly_tss_target,
            )
            success = "Training profile saved."
            p = db.get_training_profile(DB_PATH, user["id"])
        except Exception as exc:
            log.error("Training profile save error: %s", exc)
            error = "Failed to save. Please check your inputs."

    preferred_days_list = (p.get("preferred_days") or "").split(",") if p else []

    days_checkboxes = ""
    for d in _DAY_OPTIONS:
        checked = "checked" if d in preferred_days_list else ""
        days_checkboxes += f"""
<label class="flex items-center gap-2 cursor-pointer">
  <input type="checkbox" name="preferred_days" value="{d}" {checked}
    class="w-4 h-4 accent-cyan-500"/>
  <span class="text-sm text-gray-300">{d}</span>
</label>"""

    body = f"""
{_flash_msg(error)}
{_flash_msg(success, "success")}
<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Block Goal</p>
  <form method="post" class="space-y-4">
    {_select("goal", "What are you aiming to improve?", _GOAL_OPTIONS, p.get("goal",""))}
    <div>
      <label class="block text-xs text-gray-400 mb-1">Custom goal (if selected above)</label>
      <input name="goal_custom" type="text" value="{p.get('goal_custom','')}" maxlength="120"
        placeholder="Describe your goal..."
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    {_select("discipline", "Primary discipline", _DISCIPLINE_OPTIONS, p.get("discipline","road"))}
    <div>
      <label class="block text-xs text-gray-400 mb-2">Preferred riding days</label>
      <div class="grid grid-cols-4 gap-2">{days_checkboxes}</div>
    </div>
    {_input("weekly_hours_target", "Weekly hours target", "number", p.get("weekly_hours_target","") or "", "min='0' max='40' step='0.5'")}
    {_input("weekly_rides_target", "Weekly rides target", "number", p.get("weekly_rides_target","") or "", "min='0' max='20'")}
    {_input("weekly_tss_target", "Weekly TSS target (optional)", "number", p.get("weekly_tss_target","") or "", "min='0' max='2000' step='10'")}
    <div class="border-t border-gray-800 pt-4">
      <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Target Event (optional)</p>
      {_input("target_event_name", "Event name", "text", p.get("target_event_name",""), 'placeholder="e.g. Mourne 500"')}
      {_input("target_event_date", "Event date", "date", p.get("target_event_date",""))}
    </div>
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Save profile
    </button>
  </form>
</div>
"""
    return _html_response(_page("Training Profile", body, user, "settings"))


# ---------------------------------------------------------------------------
# Routes: Leaderboard
# ---------------------------------------------------------------------------

@app.route("/leaderboard")
@login_required
def leaderboard():
    user = g.user
    rows = db.get_leaderboard(DB_PATH)

    def _rank_rows(data, key, fmt_fn, reverse=True):
        sorted_data = sorted(data, key=lambda x: x.get(key, 0), reverse=reverse)
        out = ""
        for i, r in enumerate(sorted_data):
            val = r.get(key, 0)
            if val <= 0:
                continue
            medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
            out += f"""
<div class="flex items-center justify-between py-2 border-b border-gray-800 last:border-0">
  <div class="flex items-center gap-3">
    <span class="text-base w-8 text-center">{medal}</span>
    <span class="text-sm text-gray-200">{r['name']}</span>
  </div>
  <span class="text-sm font-semibold text-gray-100">{fmt_fn(val)}</span>
</div>"""
        return out or '<p class="text-sm text-gray-500 py-2">No data yet.</p>'

    def _pct(seconds):
        h = int(seconds) // 3600
        m = (int(seconds) % 3600) // 60
        return f"{h}h {m:02d}m"

    week_start = date.today() - timedelta(days=date.today().weekday())
    week_label = week_start.strftime("%-d %b")

    sections = [
        ("Weekly Distance",    "weekly_distance_m",         lambda v: fmt_dist(v)),
        ("Weekly Moving Time", "weekly_moving_time_s",       _pct),
        ("Weekly Elevation",   "weekly_elevation_m",         fmt_elev),
        ("Weekly TSS",         "weekly_tss",                 lambda v: f"{v:.0f}"),
        ("Longest Ride",       "weekly_longest_distance_m",  lambda v: fmt_dist(v)),
    ]

    cards_html = ""
    for title, key, fmt_fn in sections:
        content = _rank_rows(rows, key, fmt_fn)
        cards_html += _card(title, content)

    body = f"""
<p class="text-xs text-gray-500 mb-4">Week from {week_label}</p>
{cards_html}
"""
    return _html_response(_page("Leaderboard", body, user, ""))


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

    existing = db.get_nutrition(DB_PATH, user["id"], today)
    v = existing or {}
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
        <input name="calories" type="number" step="1" min="0" value="{v.get('calories') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Carbs (g)</label>
        <input name="carbs_g" type="number" step="0.1" min="0" value="{v.get('carbs_g') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Protein (g)</label>
        <input name="protein_g" type="number" step="0.1" min="0" value="{v.get('protein_g') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Fat (g)</label>
        <input name="fat_g" type="number" step="0.1" min="0" value="{v.get('fat_g') or ''}"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
      </div>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">Notes</label>
      <input name="notes" type="text" maxlength="200" value="{v.get('notes') or ''}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Save
    </button>
  </form>
</div>
"""

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
        <th class="pb-1 text-xs text-gray-600 font-medium text-right">Bal</th>
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
    if msg_param == "strava_ok":
        success = "Strava connected successfully!"
    elif msg_param == "strava_disconnected":
        success = "Strava disconnected. You can connect a different account below."
    elif msg_param == "strava_denied":
        error = "Strava connection was denied."
    elif msg_param == "strava_error":
        error = "Strava connection failed. Please try again."

    if request.method == "POST":
        try:
            new_ftp = float(request.form.get("ftp") or user["ftp"])
            bmr = float(request.form.get("bmr") or user["bmr"])
            calorie_target = float(request.form.get("calorie_target") or user["calorie_target"])
            name = request.form.get("name", "").strip() or user["name"]
            ftp_note = request.form.get("ftp_note", "").strip()

            if new_ftp <= 0 or new_ftp > 600:
                error = "FTP must be between 1 and 600 W."
            elif bmr <= 0 or bmr > 5000:
                error = "BMR must be between 1 and 5000 kcal."
            elif calorie_target <= 0 or calorie_target > 10000:
                error = "Calorie target must be between 1 and 10000 kcal."
            else:
                old_ftp = float(user["ftp"] or 200)
                # Record FTP change in history if it changed
                if abs(new_ftp - old_ftp) >= 1:
                    note = ftp_note or "Updated in settings"
                    db.add_ftp_entry(DB_PATH, user["id"], new_ftp, source="manual", note=note)
                db.update_user_settings(DB_PATH, user["id"], ftp=new_ftp, bmr=bmr,
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
  <form method="post" action="{url_for('disconnect_strava')}">
    <button type="submit"
      class="bg-gray-700 hover:bg-red-700 text-gray-300 hover:text-white text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors">
      Disconnect
    </button>
  </form>
</div>
"""
    else:
        strava_section = f"""
<div class="flex items-center justify-between">
  <div><p class="text-sm text-gray-400">Strava not connected</p></div>
  <a href="{url_for('connect_strava')}"
     class="bg-orange-500 hover:bg-orange-400 text-white text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors">
    Connect
  </a>
</div>
"""

    # FTP history
    ftp_history = db.get_ftp_history(DB_PATH, user["id"], limit=5)
    ftp_rows = ""
    for row in ftp_history:
        source_badge = {
            "manual":    '<span class="text-gray-500 text-xs">manual</span>',
            "initial":   '<span class="text-gray-600 text-xs">initial</span>',
            "estimated": '<span class="text-cyan-500 text-xs">estimated</span>',
            "test":      '<span class="text-green-400 text-xs">test</span>',
        }.get(row["source"], "")
        ftp_rows += f"""
<tr class="border-t border-gray-800">
  <td class="py-2 pr-3 text-xs text-gray-400">{row['effective_date']}</td>
  <td class="py-2 pr-3 text-sm font-semibold text-gray-100">{row['ftp_watts']:.0f} W</td>
  <td class="py-2 pr-3">{source_badge}</td>
  <td class="py-2 text-xs text-gray-500 truncate max-w-0" style="max-width:120px">{row['note'] or ''}</td>
</tr>
"""
    ftp_history_section = ""
    if ftp_rows:
        ftp_history_section = f"""
<div class="mt-3 overflow-x-auto">
  <p class="text-xs text-gray-600 mb-2">FTP History</p>
  <table class="w-full">
    <tbody>{ftp_rows}</tbody>
  </table>
</div>
"""

    # Admin link for admins
    admin_link = ""
    if user["is_admin"]:
        admin_link = f"""
<div class="mt-3">
  <a href="{url_for('admin')}" class="text-xs text-cyan-500 hover:text-cyan-300">⚙ Admin panel →</a>
</div>
"""

    body = f"""
{_flash_msg(error)}
{_flash_msg(success, "success")}

{_card("Strava", strava_section)}

<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Training Settings</p>
  <form method="post" class="space-y-3">
    {_input("name", "Name", "text", user['name'])}
    <div>
      <label class="block text-xs text-gray-400 mb-1">FTP (watts)</label>
      <input name="ftp" type="number" step="1" min="50" max="600" value="{user['ftp']:.0f}"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    <div>
      <label class="block text-xs text-gray-400 mb-1">FTP update note (optional)</label>
      <input name="ftp_note" type="text" maxlength="100" placeholder="e.g. Post 20min test"
        class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-gray-100 text-sm focus:outline-none focus:border-cyan-500"/>
    </div>
    {_input("bmr", "BMR (kcal/day)", "number", f"{user['bmr']:.0f}", "step='1' min='500' max='5000'")}
    {_input("calorie_target", "Daily calorie target (kcal)", "number", f"{user['calorie_target']:.0f}", "step='1' min='500' max='10000'")}
    <button type="submit"
      class="w-full bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-2 rounded-lg text-sm transition-colors">
      Save settings
    </button>
  </form>
  {ftp_history_section}
</div>

<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <div class="flex items-center justify-between">
    <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Training Profile</p>
    <a href="{url_for('training_profile')}" class="text-xs text-cyan-500 hover:text-cyan-300">Edit →</a>
  </div>
</div>

<div class="bg-gray-900 rounded-xl p-4 mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Account</p>
  <p class="text-sm text-gray-400 mb-1">{user['email']}</p>
  <p class="text-xs text-gray-600">Member since {user['created_at'][:10]}</p>
  {admin_link}
</div>
"""
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
    elif msg == "sync_started":
        success = "Sync started in the background."

    rows_html = ""
    for u in users:
        mc = db.get_metrics_cache(DB_PATH, u["id"])
        coaching = db.get_coaching_cache(DB_PATH, u["id"])
        has_strava = db.get_strava_tokens(DB_PATH, u["id"]) is not None
        profile = db.get_training_profile(DB_PATH, u["id"])

        classification = coaching.get("classification", "—") if coaching else "—"
        ctl = coaching.get("ctl", 0) if coaching else 0
        atl = coaching.get("atl", 0) if coaching else 0
        tsb = coaching.get("tsb", 0) if coaching else 0
        tsb_sign = "+" if tsb >= 0 else ""

        sc = status_color(classification)
        sb = status_bg(classification)

        weekly_dist = fmt_dist(mc["weekly_distance_m"]) if mc else "—"
        weekly_tss = f"{mc['weekly_tss']:.0f}" if mc and "weekly_tss" in mc.keys() and mc["weekly_tss"] else "—"

        goal = profile.get("goal", "") if profile else ""
        goal_custom = profile.get("goal_custom", "") if profile else ""
        effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal
        goal_html = f'<p class="text-xs text-gray-500 truncate" style="max-width:200px">{effective_goal}</p>' if effective_goal else ""

        strava_badge = (
            '<span class="text-green-400 text-xs">✓ Strava</span>'
            if has_strava
            else '<span class="text-gray-600 text-xs">No Strava</span>'
        )
        admin_badge = '<span class="text-cyan-400 text-xs ml-1">Admin</span>' if u["is_admin"] else ""
        updated = mc["updated_at"][:10] if mc and mc["updated_at"] else "—"
        current_ftp = db.get_current_ftp(DB_PATH, u["id"])

        rows_html += f"""
<div class="bg-gray-900 rounded-xl p-4 mb-2">
  <div class="flex items-start justify-between mb-2">
    <div>
      <p class="text-sm font-semibold text-gray-100">{u['name']}{admin_badge}</p>
      <p class="text-xs text-gray-500">{u['email']}</p>
      {goal_html}
    </div>
    <span class="inline-block px-2 py-0.5 rounded-full text-xs font-semibold {sc} {sb} ml-2 flex-shrink-0">{classification}</span>
  </div>
  <div class="grid grid-cols-5 gap-1 text-center mb-2">
    <div>
      <p class="text-sm font-bold text-blue-400">{ctl:.0f}</p>
      <p class="text-xs text-gray-600">CTL</p>
    </div>
    <div>
      <p class="text-sm font-bold text-purple-400">{atl:.0f}</p>
      <p class="text-xs text-gray-600">ATL</p>
    </div>
    <div>
      <p class="text-sm font-bold {'text-green-400' if tsb >= 0 else 'text-red-400'}">{tsb_sign}{tsb:.0f}</p>
      <p class="text-xs text-gray-600">TSB</p>
    </div>
    <div>
      <p class="text-sm font-bold text-gray-300">{current_ftp:.0f}</p>
      <p class="text-xs text-gray-600">FTP</p>
    </div>
    <div>
      <p class="text-sm font-bold text-yellow-400">{weekly_tss}</p>
      <p class="text-xs text-gray-600">Wk TSS</p>
    </div>
  </div>
  <div class="flex items-center justify-between">
    <div class="flex items-center gap-3">
      {strava_badge}
      <span class="text-gray-600 text-xs">{weekly_dist} this week</span>
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
<div class="flex items-center justify-between mb-3">
  <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Members ({len(users)})</p>
  <a href="{url_for('leaderboard')}" class="text-xs text-cyan-500 hover:text-cyan-300">Leaderboard →</a>
</div>
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
            ftp = db.get_current_ftp(db_path, user_id, fallback=200.0)
            values = metrics_mod.compute_metrics(activities, ftp)
            db.save_metrics_cache(db_path, user_id, values)
            today_str = date.today().isoformat()
            training_profile = db.get_training_profile(db_path, user_id)
            planned_workout = db.get_today_workout(db_path, user_id, today_str)
            pw_dict = dict(planned_workout) if planned_workout else None
            state = coach_mod.TrainingState.from_metrics(values)
            result = coach_mod.evaluate(state, training_profile=training_profile, planned_workout=pw_dict)
            db.save_coaching_cache(db_path, user_id, result.to_dict())
            log.info("Manual sync complete for user %s", user_id)
        except Exception as exc:
            log.error("Manual sync failed for user %s: %s", user_id, exc)

    t = threading.Thread(
        target=_do_sync,
        args=(DB_PATH, uid, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET),
        daemon=True
    )
    t.start()
    return redirect(url_for("admin") + "?msg=sync_started")


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


start_poller()


if __name__ == "__main__":
    start_poller()
    app.run(host="0.0.0.0", port=9206, debug=False)
