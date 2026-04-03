"""
reconciliation.py - Plan vs actual ride comparison and ride style suggestion.
Deterministic, no AI. Logic driven by TSB band + recent ride pattern + training goal.
Outdoor-friendly ride styles, not structured intervals.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
from datetime import date as _date, timedelta as _td

# ---------------------------------------------------------------------------
# Reconciliation states
# ---------------------------------------------------------------------------
PLAN_COMPLETED      = "PLAN_COMPLETED"
PLAN_OVERPERFORMED  = "PLAN_OVERPERFORMED"
PLAN_UNDERPERFORMED = "PLAN_UNDERPERFORMED"
PLAN_SKIPPED        = "PLAN_SKIPPED"
UNPLANNED_RIDE      = "UNPLANNED_RIDE"

RECON_LABELS = {
    PLAN_COMPLETED:      ("Completed",      "text-green-400",  "✓"),
    PLAN_OVERPERFORMED:  ("Overperformed",  "text-cyan-400",   "↑"),
    PLAN_UNDERPERFORMED: ("Underperformed", "text-yellow-400", "↓"),
    PLAN_SKIPPED:        ("Skipped",        "text-gray-500",   "–"),
    UNPLANNED_RIDE:      ("Unplanned ride", "text-blue-400",   "~"),
}

# ---------------------------------------------------------------------------
# Ride styles — outdoor formats only, no structured intervals
# ---------------------------------------------------------------------------
EASY_SPIN          = "Easy Spin"
ENDURANCE_RIDE     = "Endurance Ride"
ROLLING_ENDURANCE  = "Rolling Endurance Ride"
LONG_ENDURANCE     = "Long Endurance Ride"
HILL_EFFORTS       = "Hill Efforts"
TEMPO_TERRAIN      = "Tempo Terrain Ride"
REST_DAY           = "Rest Day"

# Backward-compat alias (old coaching_cache entries may use this)
RECOVERY_SPIN = "Recovery Spin"

# Default duration (min) and TSS per style
_STYLE_DEFAULTS = {
    EASY_SPIN:         {"duration_min": 50,  "tss": 20},
    RECOVERY_SPIN:     {"duration_min": 50,  "tss": 25},
    ENDURANCE_RIDE:    {"duration_min": 90,  "tss": 65},
    ROLLING_ENDURANCE: {"duration_min": 100, "tss": 75},
    LONG_ENDURANCE:    {"duration_min": 150, "tss": 100},
    HILL_EFFORTS:      {"duration_min": 90,  "tss": 85},
    TEMPO_TERRAIN:     {"duration_min": 80,  "tss": 90},
    REST_DAY:          {"duration_min": 0,   "tss": 0},
}

# Icons for UI display
STYLE_ICONS = {
    EASY_SPIN:         "💨",
    RECOVERY_SPIN:     "💨",
    ENDURANCE_RIDE:    "🚴",
    ROLLING_ENDURANCE: "🌄",
    LONG_ENDURANCE:    "🛣️",
    HILL_EFFORTS:      "⛰️",
    TEMPO_TERRAIN:     "⚡",
    REST_DAY:          "😴",
}

# Terrain-driven ride hints — how to ride it, not interval prescriptions
STYLE_HINTS = {
    EASY_SPIN:
        "Spin easy on familiar roads. Keep the effort fully conversational throughout.",
    RECOVERY_SPIN:
        "Keep effort minimal — if you're breathing hard, you're going too fast.",
    ENDURANCE_RIDE:
        "Steady effort at a pace you could hold all day. No need to push.",
    ROLLING_ENDURANCE:
        "Pick a route with some gentle hills and let the terrain vary the effort naturally.",
    LONG_ENDURANCE:
        "Get out for a longer ride at a steady, sustainable pace. Take food and enjoy it.",
    HILL_EFFORTS:
        "Find 4–6 climbs lasting 3–6 minutes and ride them with purpose. Easy between each.",
    TEMPO_TERRAIN:
        "Pick a rolling or undulating route and ride at a sustained, comfortably hard effort.",
    REST_DAY:
        "Off the bike today. Stretch, walk, or simply rest.",
}

# ---------------------------------------------------------------------------
# Thresholds used in pattern analysis
# ---------------------------------------------------------------------------
_HARD_TSS = 70         # rides above this count as a "hard effort"
_BIG_RIDE_TSS = 100    # yesterday at this TSS or above → favour easy today
_LONG_RIDE_S = 5400    # 90 min: threshold for "had a long ride this week"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    state: Optional[str] = None
    planned_tss: float = 0.0
    actual_tss: float = 0.0
    planned_duration_minutes: int = 0
    actual_duration_minutes: int = 0
    planned_title: str = ""
    actual_name: str = ""

    def to_dict(self) -> dict:
        return {
            "recon_state":                self.state,
            "recon_planned_tss":          self.planned_tss,
            "recon_actual_tss":           self.actual_tss,
            "recon_planned_duration_min": self.planned_duration_minutes,
            "recon_actual_duration_min":  self.actual_duration_minutes,
            "recon_planned_title":        self.planned_title,
            "recon_actual_name":          self.actual_name,
        }


# ---------------------------------------------------------------------------
# Plan reconciliation
# ---------------------------------------------------------------------------

def evaluate_plan_reconciliation(
    planned_workout: Optional[dict],
    actual_rides: List[dict],
) -> ReconciliationResult:
    """
    Compare a planned workout against actual rides on the same date.

    planned_workout: dict from DB planned_workouts table, or None
    actual_rides: list of activity_log row dicts for that date
    """
    planned_tss = float(planned_workout.get("target_tss") or 0) if planned_workout else 0.0
    planned_dur = int(planned_workout.get("target_duration_min") or 0) if planned_workout else 0
    planned_title = planned_workout.get("title", "") if planned_workout else ""

    actual_tss = sum(float(r.get("tss") or 0) for r in actual_rides)
    actual_dur = sum(int(float(r.get("moving_time_s") or 0) / 60) for r in actual_rides)
    actual_name = actual_rides[0].get("name", "") if actual_rides else ""

    has_plan = bool(planned_workout)
    has_ride = bool(actual_rides)

    if not has_plan and not has_ride:
        state = None
    elif has_ride and not has_plan:
        state = UNPLANNED_RIDE
    elif has_plan and not has_ride:
        state = PLAN_SKIPPED
    else:
        if planned_tss > 0:
            ratio = actual_tss / planned_tss
            if ratio >= 1.2:
                state = PLAN_OVERPERFORMED
            elif ratio >= 0.75:
                state = PLAN_COMPLETED
            else:
                state = PLAN_UNDERPERFORMED
        else:
            state = PLAN_COMPLETED

    return ReconciliationResult(
        state=state,
        planned_tss=planned_tss,
        actual_tss=actual_tss,
        planned_duration_minutes=planned_dur,
        actual_duration_minutes=actual_dur,
        planned_title=planned_title,
        actual_name=actual_name,
    )


# ---------------------------------------------------------------------------
# Recent pattern analysis
# ---------------------------------------------------------------------------

def _analyse_pattern(recent_rides: list) -> dict:
    """
    Derive lightweight training pattern signals from recent activity_log entries.
    recent_rides: list of dicts (from db.get_recent_activities), any order.
    """
    today = _date.today()
    week_start = today - _td(days=today.weekday())
    yesterday = (today - _td(days=1)).isoformat()

    # Sort newest first
    rides = sorted(recent_rides, key=lambda r: r.get("date", ""), reverse=True)

    # Yesterday's total TSS
    yesterday_tss = sum(
        float(r.get("tss") or 0) for r in rides if r.get("date") == yesterday
    )

    # Hard effort frequency in the last 5 rides
    last_5 = rides[:5]
    hard_count_last5 = sum(1 for r in last_5 if float(r.get("tss") or 0) >= _HARD_TSS)

    # Days since most recent hard effort
    days_since_hard: Optional[int] = None
    for r in rides:
        if float(r.get("tss") or 0) >= _HARD_TSS:
            try:
                ride_date = _date.fromisoformat(r["date"])
                days_since_hard = (today - ride_date).days
            except (ValueError, KeyError):
                pass
            break

    # Long ride this week (≥90 min moving time)
    had_long_ride_this_week = any(
        float(r.get("moving_time_s") or 0) >= _LONG_RIDE_S
        for r in rides
        if r.get("date", "") >= week_start.isoformat()
    )

    # Days since last ride of any kind
    days_since_last: Optional[int] = None
    if rides:
        try:
            days_since_last = (today - _date.fromisoformat(rides[0]["date"])).days
        except (ValueError, KeyError):
            pass

    return {
        "yesterday_tss":          yesterday_tss,
        "hard_count_last5":       hard_count_last5,
        "days_since_hard":        days_since_hard,
        "had_long_ride_this_week": had_long_ride_this_week,
        "days_since_last_ride":   days_since_last,
    }


# ---------------------------------------------------------------------------
# Ride style suggestion
# ---------------------------------------------------------------------------

def suggest_ride(
    classification: str,
    tsb: float,
    weekly_hours: float,
    weekly_tss: float,
    training_profile: Optional[dict] = None,
    yesterday_recon: Optional[ReconciliationResult] = None,
    today_plan: Optional[dict] = None,
    recent_rides: Optional[list] = None,
) -> dict:
    """
    Return a ride style suggestion dict for merging into coaching_cache.

    Keys: ride_style, suggested_duration_minutes, suggested_tss,
          ride_style_rationale, ride_hint
    """
    p = training_profile or {}
    goal = p.get("goal", "")
    goal_custom = p.get("goal_custom", "")
    effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal
    weekly_hours_target = float(p.get("weekly_hours_target") or 0)
    target_event_date = p.get("target_event_date", "")

    pattern = _analyse_pattern(recent_rides or [])

    # -----------------------------------------------------------------
    # 1. Base style from TSB classification
    # -----------------------------------------------------------------
    style = _base_style(classification)

    # -----------------------------------------------------------------
    # 2. Pattern overrides — applied before goal modifier
    #    These take priority because they reflect concrete recent data.
    # -----------------------------------------------------------------

    # CASE 1: Heavy riding recently + big effort yesterday → easy day
    if (classification in ("Productive Fatigue", "Heavy Load", "Very Fatigued")
            and pattern["yesterday_tss"] >= _BIG_RIDE_TSS):
        style = EASY_SPIN

    # CASE 2: Moderate fatigue but no hard effort recently
    #         → a few controlled hill efforts is appropriate stimulus
    elif (classification in ("Productive Fatigue", "Slight Fatigue")
          and pattern["hard_count_last5"] == 0
          and (pattern["days_since_hard"] is None or pattern["days_since_hard"] >= 5)):
        style = HILL_EFFORTS

    # CASE 3: Fresh/Ready + no long ride this week → priority for long endurance
    elif (classification in ("Fresh", "Ready")
          and not pattern["had_long_ride_this_week"]):
        style = LONG_ENDURANCE

    # -----------------------------------------------------------------
    # 3. Goal modifier (only when not already on an easy/rest day)
    # -----------------------------------------------------------------
    if style not in (REST_DAY, RECOVERY_SPIN, EASY_SPIN):
        style = _apply_goal(style, classification, effective_goal, target_event_date)

    # -----------------------------------------------------------------
    # 4. Downgrade if weekly volume is well above target
    # -----------------------------------------------------------------
    if weekly_hours_target > 0 and weekly_hours > weekly_hours_target * 1.25:
        style = _downgrade(style)

    # -----------------------------------------------------------------
    # 5. Honour today's planned workout when readiness allows
    # -----------------------------------------------------------------
    if today_plan and style not in (REST_DAY, RECOVERY_SPIN, EASY_SPIN):
        plan_style = _plan_type_to_style(today_plan.get("type", ""))
        if plan_style and _safe_for_readiness(plan_style, classification):
            style = plan_style

    # -----------------------------------------------------------------
    # 6. Duration and TSS
    # -----------------------------------------------------------------
    defaults = _STYLE_DEFAULTS.get(style, _STYLE_DEFAULTS[ENDURANCE_RIDE])
    duration_min = defaults["duration_min"]
    tss = defaults["tss"]

    # Scale down if weekly target is modest
    if weekly_hours_target > 0 and style not in (REST_DAY, RECOVERY_SPIN, EASY_SPIN):
        max_min = int((weekly_hours_target / 4.0) * 60)
        if 30 <= max_min < duration_min:
            tss = int(tss * max_min / duration_min)
            duration_min = max_min

    rationale = _rationale(style, classification, effective_goal,
                            pattern, yesterday_recon, today_plan)
    hint = STYLE_HINTS.get(style, "")

    return {
        "ride_style":                style,
        "suggested_duration_minutes": duration_min,
        "suggested_tss":             tss,
        "ride_style_rationale":      rationale,
        "ride_hint":                 hint,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_style(classification: str) -> str:
    return {
        "Fresh":              HILL_EFFORTS,
        "Ready":              ROLLING_ENDURANCE,
        "Slight Fatigue":     ENDURANCE_RIDE,
        "Productive Fatigue": ENDURANCE_RIDE,
        "Heavy Load":         EASY_SPIN,
        "Very Fatigued":      EASY_SPIN,
        "Deep Fatigue":       REST_DAY,
    }.get(classification, ENDURANCE_RIDE)


def _apply_goal(style: str, classification: str,
                goal: str, target_event_date: str) -> str:
    if goal == "Build aerobic base":
        # Always favour endurance for base building, but long endurance stays
        return style if style == LONG_ENDURANCE else ENDURANCE_RIDE

    if goal == "Raise FTP / threshold":
        if classification in ("Fresh", "Ready"):
            return TEMPO_TERRAIN
        if classification in ("Slight Fatigue", "Productive Fatigue"):
            return ENDURANCE_RIDE  # absorb load first

    if goal == "Prepare for an event" and target_event_date:
        try:
            days_left = (_date.fromisoformat(target_event_date) - _date.today()).days
            if 0 <= days_left <= 3:
                return EASY_SPIN
            if days_left <= 7:
                return ENDURANCE_RIDE
        except ValueError:
            pass

    if goal == "Improve endurance for longer rides":
        if classification in ("Fresh", "Ready", "Slight Fatigue"):
            return style if style == LONG_ENDURANCE else ENDURANCE_RIDE

    return style


def _downgrade(style: str) -> str:
    chain = [HILL_EFFORTS, TEMPO_TERRAIN, LONG_ENDURANCE,
             ROLLING_ENDURANCE, ENDURANCE_RIDE, EASY_SPIN]
    idx = chain.index(style) if style in chain else -1
    if 0 <= idx < len(chain) - 1:
        return chain[idx + 1]
    return style


def _plan_type_to_style(plan_type: str) -> Optional[str]:
    return {
        "endurance": ENDURANCE_RIDE,
        "tempo":     TEMPO_TERRAIN,
        "threshold": TEMPO_TERRAIN,
        "vo2":       HILL_EFFORTS,
        "recovery":  EASY_SPIN,
        "rest":      REST_DAY,
    }.get(plan_type)


def _safe_for_readiness(style: str, classification: str) -> bool:
    if style in (HILL_EFFORTS, TEMPO_TERRAIN):
        return classification in ("Fresh", "Ready", "Slight Fatigue", "Productive Fatigue")
    return True


def _rationale(
    style: str,
    classification: str,
    goal: str,
    pattern: dict,
    yesterday_recon: Optional[ReconciliationResult],
    today_plan: Optional[dict],
) -> str:
    parts = []

    yesterday_tss = pattern.get("yesterday_tss", 0)
    days_since_hard = pattern.get("days_since_hard")
    had_long_ride = pattern.get("had_long_ride_this_week", False)

    # --- Lead sentence: what the body is doing right now ---
    if style == EASY_SPIN and yesterday_tss >= _BIG_RIDE_TSS:
        parts.append(
            "Legs are carrying some load from recent riding. "
            "An easy spin today will help absorb the work — "
            "but if you feel good out there, a relaxed endurance ride is fine too."
        )
    elif style == EASY_SPIN:
        parts.append(
            "Fatigue is elevated right now. "
            "Keeping today easy will let the training bed in properly."
        )
    elif style == REST_DAY:
        parts.append(
            "Body is asking for a rest today. "
            "Taking it easy will set you up better for the next session."
        )
    elif style == HILL_EFFORTS and classification in ("Productive Fatigue", "Slight Fatigue"):
        parts.append(
            "You're carrying some training load, but it's been a while since your last harder effort. "
            "A few controlled hill efforts could work well today — keep them steady, not all-out."
        )
    elif style == HILL_EFFORTS:
        parts.append("You're well rested — a good window for some quality hill efforts.")
    elif style == LONG_ENDURANCE and not had_long_ride:
        parts.append(
            "You're fresh and there hasn't been a longer ride this week. "
            "Good conditions to get out for an extended endurance ride."
        )
    elif style == TEMPO_TERRAIN:
        parts.append(
            "You're well rested — a good window for some quality riding. "
            "Find terrain that lets you push at a sustained effort."
        )
    elif style == ROLLING_ENDURANCE:
        parts.append("Ready to train. A rolling endurance ride is a solid choice today.")
    else:
        # Generic endurance
        state_text = {
            "Fresh":              "You're well rested",
            "Ready":              "You're ready to train",
            "Slight Fatigue":     "Legs are carrying some fatigue",
            "Productive Fatigue": "You're in a solid loading phase",
            "Heavy Load":         "Load is building",
        }.get(classification, "")
        if state_text:
            parts.append(f"{state_text} — a steady endurance ride is the right call today.")

    # --- Goal context (brief, when it changes the suggestion) ---
    if goal and style not in (REST_DAY, EASY_SPIN, RECOVERY_SPIN):
        if goal == "Build aerobic base" and style == ENDURANCE_RIDE:
            parts.append("This supports your aerobic base goal.")
        elif goal == "Raise FTP / threshold" and style == TEMPO_TERRAIN:
            parts.append("Targeting your FTP goal.")
        elif goal == "Raise FTP / threshold" and style == ENDURANCE_RIDE:
            parts.append("Legs need a little more recovery before quality FTP work pays off.")
        elif goal == "Prepare for an event":
            parts.append("Keeping event prep on track.")
        elif goal == "Improve endurance for longer rides" and style == LONG_ENDURANCE:
            parts.append("Building towards longer rides.")

    # --- Yesterday note (only if it adds context) ---
    if yesterday_recon and yesterday_recon.state:
        note = {
            PLAN_OVERPERFORMED:  "Yesterday you went beyond the plan — no need to push again today.",
            PLAN_SKIPPED:        "Yesterday's planned session was missed.",
        }.get(yesterday_recon.state, "")
        if note:
            parts.append(note)

    return " ".join(parts)
