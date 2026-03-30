"""
reconciliation.py - Plan vs actual ride comparison and ride style suggestion.
Deterministic, no AI. All logic is rule-based from TSB + training profile + plan.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import date as _date

# ---------------------------------------------------------------------------
# Reconciliation states
# ---------------------------------------------------------------------------
PLAN_COMPLETED     = "PLAN_COMPLETED"
PLAN_OVERPERFORMED = "PLAN_OVERPERFORMED"
PLAN_UNDERPERFORMED = "PLAN_UNDERPERFORMED"
PLAN_SKIPPED       = "PLAN_SKIPPED"
UNPLANNED_RIDE     = "UNPLANNED_RIDE"

# Human-readable labels and colours per state
RECON_LABELS = {
    PLAN_COMPLETED:     ("Completed",      "text-green-400",  "✓"),
    PLAN_OVERPERFORMED: ("Overperformed",  "text-cyan-400",   "↑"),
    PLAN_UNDERPERFORMED:("Underperformed", "text-yellow-400", "↓"),
    PLAN_SKIPPED:       ("Skipped",        "text-gray-500",   "–"),
    UNPLANNED_RIDE:     ("Unplanned ride", "text-blue-400",   "~"),
}

# ---------------------------------------------------------------------------
# Ride styles
# ---------------------------------------------------------------------------
ENDURANCE_RIDE = "Endurance Ride"
HILL_EFFORTS   = "Hill Efforts"
TEMPO_TERRAIN  = "Tempo Terrain Ride"
RECOVERY_SPIN  = "Recovery Spin"
REST_DAY       = "Rest Day"

# Default duration (min) and TSS per style
_STYLE_DEFAULTS = {
    ENDURANCE_RIDE: {"duration_min": 90,  "tss": 65},
    HILL_EFFORTS:   {"duration_min": 90,  "tss": 85},
    TEMPO_TERRAIN:  {"duration_min": 80,  "tss": 90},
    RECOVERY_SPIN:  {"duration_min": 50,  "tss": 25},
    REST_DAY:       {"duration_min": 0,   "tss": 0},
}

# Ride style icons for UI
STYLE_ICONS = {
    ENDURANCE_RIDE: "🚴",
    HILL_EFFORTS:   "⛰",
    TEMPO_TERRAIN:  "⚡",
    RECOVERY_SPIN:  "💚",
    REST_DAY:       "😴",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    state: Optional[str]             # one of the state constants above, or None
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
        # Both plan and ride — compare TSS if target was set
        if planned_tss > 0:
            ratio = actual_tss / planned_tss
            if ratio >= 1.2:
                state = PLAN_OVERPERFORMED
            elif ratio >= 0.75:
                state = PLAN_COMPLETED
            else:
                state = PLAN_UNDERPERFORMED
        else:
            # No TSS target: presence of any ride = completed
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
) -> dict:
    """
    Return a ride style suggestion dict for merging into coaching_cache.

    Keys: ride_style, suggested_duration_minutes, suggested_tss, ride_style_rationale
    """
    p = training_profile or {}
    goal = p.get("goal", "")
    goal_custom = p.get("goal_custom", "")
    effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal
    weekly_hours_target = float(p.get("weekly_hours_target") or 0)
    target_event_date = p.get("target_event_date", "")

    # 1. Base style from TSB classification
    style = _base_style(classification)

    # 2. Apply goal modifier (only when not already rest/recovery)
    if style not in (REST_DAY, RECOVERY_SPIN):
        style = _apply_goal(style, classification, effective_goal, target_event_date)

    # 3. Downgrade if weekly volume is already well above target
    if weekly_hours_target > 0 and weekly_hours > weekly_hours_target * 1.25:
        style = _downgrade(style)

    # 4. If today has a planned workout, honour it when readiness allows
    if today_plan and style not in (REST_DAY, RECOVERY_SPIN):
        plan_style = _plan_type_to_style(today_plan.get("type", ""))
        if plan_style and _safe_for_readiness(plan_style, classification):
            style = plan_style

    # 5. Derive duration and TSS
    defaults = _STYLE_DEFAULTS[style]
    duration_min = defaults["duration_min"]
    tss = defaults["tss"]

    # Scale down if weekly target is modest and session would be too long
    if weekly_hours_target > 0 and style not in (REST_DAY, RECOVERY_SPIN):
        max_min = int((weekly_hours_target / 4.0) * 60)
        if 30 <= max_min < duration_min:
            tss = int(tss * max_min / duration_min)
            duration_min = max_min

    rationale = _rationale(style, classification, effective_goal,
                           yesterday_recon, today_plan)

    return {
        "ride_style":                style,
        "suggested_duration_minutes": duration_min,
        "suggested_tss":             tss,
        "ride_style_rationale":      rationale,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_style(classification: str) -> str:
    return {
        "Fresh":              HILL_EFFORTS,
        "Ready":              TEMPO_TERRAIN,
        "Slight Fatigue":     ENDURANCE_RIDE,
        "Productive Fatigue": ENDURANCE_RIDE,
        "Heavy Load":         RECOVERY_SPIN,
        "Very Fatigued":      RECOVERY_SPIN,
        "Deep Fatigue":       REST_DAY,
    }.get(classification, ENDURANCE_RIDE)


def _apply_goal(style: str, classification: str,
                goal: str, target_event_date: str) -> str:
    if goal == "Build aerobic base":
        return ENDURANCE_RIDE

    if goal == "Raise FTP / threshold":
        if classification in ("Fresh", "Ready"):
            return TEMPO_TERRAIN
        if classification in ("Slight Fatigue", "Productive Fatigue"):
            return ENDURANCE_RIDE

    if goal == "Prepare for an event" and target_event_date:
        try:
            days_left = (_date.fromisoformat(target_event_date) - _date.today()).days
            if 0 <= days_left <= 3:
                return RECOVERY_SPIN
            if days_left <= 7:
                return ENDURANCE_RIDE
        except ValueError:
            pass

    if goal == "Improve endurance for longer rides":
        if classification in ("Fresh", "Ready", "Slight Fatigue"):
            return ENDURANCE_RIDE

    return style


def _downgrade(style: str) -> str:
    chain = [HILL_EFFORTS, TEMPO_TERRAIN, ENDURANCE_RIDE, RECOVERY_SPIN]
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
        "recovery":  RECOVERY_SPIN,
        "rest":      REST_DAY,
    }.get(plan_type)


def _safe_for_readiness(style: str, classification: str) -> bool:
    """Return True if readiness level is adequate for the given style."""
    if style in (HILL_EFFORTS, TEMPO_TERRAIN):
        return classification in ("Fresh", "Ready", "Slight Fatigue", "Productive Fatigue")
    return True


def _rationale(
    style: str,
    classification: str,
    goal: str,
    yesterday_recon: Optional[ReconciliationResult],
    today_plan: Optional[dict],
) -> str:
    parts = []

    # Lead with TSB state
    state_text = {
        "Fresh":              "You're well rested",
        "Ready":              "You're ready to train",
        "Slight Fatigue":     "You're carrying slight fatigue",
        "Productive Fatigue": "You're in a productive loading phase",
        "Heavy Load":         "Your load is high",
        "Very Fatigued":      "You're quite fatigued",
        "Deep Fatigue":       "You're deeply fatigued",
    }.get(classification, "Based on your current load")

    style_text = {
        HILL_EFFORTS:   "— a good window for hill efforts",
        TEMPO_TERRAIN:  "— well placed for a tempo terrain ride",
        ENDURANCE_RIDE: "— steady endurance is the right call",
        RECOVERY_SPIN:  "— keep it easy today",
        REST_DAY:       "— rest is strongly recommended",
    }.get(style, "")

    parts.append(state_text + " " + style_text)

    # Goal context
    if goal and style not in (REST_DAY, RECOVERY_SPIN):
        if goal == "Build aerobic base":
            parts.append("Supporting your aerobic base goal.")
        elif goal == "Raise FTP / threshold" and style == TEMPO_TERRAIN:
            parts.append("Targeting your FTP goal.")
        elif goal == "Prepare for an event":
            parts.append("Keeping event prep on track.")
        elif goal == "Improve endurance for longer rides" and style == ENDURANCE_RIDE:
            parts.append("Building endurance for longer rides.")

    # Yesterday note
    if yesterday_recon and yesterday_recon.state:
        note = {
            PLAN_COMPLETED:      "Yesterday's session was completed — good work.",
            PLAN_OVERPERFORMED:  "Yesterday you went beyond the plan — don't over-extend today.",
            PLAN_UNDERPERFORMED: "Yesterday's effort came in below the plan.",
            PLAN_SKIPPED:        "Yesterday's planned session was missed.",
            UNPLANNED_RIDE:      "Yesterday's ride was unplanned.",
        }.get(yesterday_recon.state, "")
        if note:
            parts.append(note)

    return " ".join(parts)
