"""
coaching.py - Deterministic TSB-based coaching engine.
7-band TSB classification. Fatigue ratio provides supplementary flags only.
Training profile and planned workout provide optional context — never invented.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from datetime import date as _date


@dataclass
class TrainingState:
    ctl: float
    atl: float
    tsb: float
    daily_tss: float = 0.0
    rolling_7d_load: float = 0.0
    rolling_7d_hours: float = 0.0
    calorie_balance: float = 0.0
    calories_today: float = 0.0

    @property
    def fatigue_ratio(self):
        return (self.atl / self.ctl) if self.ctl >= 5 else 0.0

    @classmethod
    def from_metrics(cls, m: dict, nutrition_balance: float = 0.0, calories_today: float = 0.0):
        return cls(
            ctl=float(m.get("cycling_ctl", 0)),
            atl=float(m.get("cycling_atl", 0)),
            tsb=float(m.get("cycling_tsb", 0)),
            daily_tss=float(m.get("cycling_daily_tss", 0)),
            rolling_7d_load=float(m.get("cycling_rolling_7d_training_load_estimate", 0)),
            rolling_7d_hours=float(m.get("cycling_rolling_7d_moving_time_seconds", 0)) / 3600.0,
            calorie_balance=nutrition_balance,
            calories_today=calories_today,
        )


@dataclass
class CoachingResult:
    classification: str
    recommendation: str
    readiness_score: float
    headline: str
    explanation: str
    risk_flag: Optional[str]
    flags: list
    insights: list
    ctl: float
    atl: float
    tsb: float
    fatigue_ratio: float
    tss_today: float

    def to_dict(self):
        return self.__dict__


STATUS_COLOR = {
    "Fresh":              "#4caf50",
    "Ready":              "#26c6da",
    "Slight Fatigue":     "#80cbc4",
    "Productive Fatigue": "#ffb300",
    "Heavy Load":         "#ff7043",
    "Very Fatigued":      "#ff5722",
    "Deep Fatigue":       "#f44336",
}

# Session types that require good readiness
_HARD_SESSION_TYPES = {"threshold", "vo2", "race"}
_EASY_SESSION_TYPES = {"recovery", "rest"}


def classify_tsb(tsb: float) -> tuple:
    """Return (classification, short_recommendation) from TSB value."""
    if tsb >= 10:
        return "Fresh", "You're fresh — a harder session would do you good"
    elif tsb >= 0:
        return "Ready", "Ready to train — get out and ride"
    elif tsb >= -10:
        return "Slight Fatigue", "Legs are ticking over — steady riding is fine"
    elif tsb >= -25:
        return "Productive Fatigue", "Carrying some load — steady work is still productive"
    elif tsb >= -40:
        return "Heavy Load", "Heavy load — keep it controlled today"
    elif tsb >= -60:
        return "Very Fatigued", "Legs are well worked — easy today pays off later"
    else:
        return "Deep Fatigue", "Body is asking for rest. Take it."


def readiness_from_tsb(tsb: float) -> float:
    """Linear readiness score 0–100 from TSB. 0 at TSB=-60, 100 at TSB=+20."""
    return round(max(0.0, min(100.0, ((tsb + 60) / 80.0) * 100.0)), 1)


def evaluate(
    s: TrainingState,
    training_profile: Optional[dict] = None,
    planned_workout: Optional[dict] = None,
) -> CoachingResult:
    classification, recommendation = classify_tsb(s.tsb)
    readiness = readiness_from_tsb(s.tsb)
    ratio = s.fatigue_ratio
    p = training_profile or {}
    pw = planned_workout or {}

    # --- Flags (load spike only) ---
    flags = []
    if ratio > 2.0:
        flags.append(f"Recent load is {ratio:.1f}× your fitness base — the work is going in, just make sure recovery keeps up.")
    elif ratio > 1.5:
        flags.append(f"Load is running a little ahead of fitness right now (ratio {ratio:.2f}) — worth keeping an eye on.")

    # --- Headlines and explanations ---
    headlines = {
        "Fresh":              "Good window for quality",
        "Ready":              "Ready to train",
        "Slight Fatigue":     "Legs are ticking over",
        "Productive Fatigue": "Carrying good training load",
        "Heavy Load":         "Heavy load — keep it controlled",
        "Very Fatigued":      "Legs are well worked",
        "Deep Fatigue":       "Rest today",
    }

    explanations = {
        "Fresh":              f"TSB {s.tsb:+.0f} — fitness ({s.ctl:.0f} CTL) is well expressed and fatigue is low. A good day to push.",
        "Ready":              f"TSB {s.tsb:+.0f} — fitness and fatigue are nicely balanced ({s.ctl:.0f} CTL / {s.atl:.0f} ATL). Conditions are good.",
        "Slight Fatigue":     f"TSB {s.tsb:+.0f} — a small fatigue deficit, which is a normal and productive training state.",
        "Productive Fatigue": f"TSB {s.tsb:+.0f} — you're in a solid loading phase. Recent load ({s.atl:.0f} ATL) is ahead of your base ({s.ctl:.0f} CTL).",
        "Heavy Load":         f"TSB {s.tsb:+.0f} — recent load ({s.atl:.0f} ATL) has built above your fitness base ({s.ctl:.0f} CTL). A lighter session will help absorb the work.",
        "Very Fatigued":      f"TSB {s.tsb:+.0f} — accumulated load ({s.atl:.0f} ATL) is well ahead of your base ({s.ctl:.0f} CTL). Letting it settle will pay off.",
        "Deep Fatigue":       f"TSB {s.tsb:+.0f} — fatigue ({s.atl:.0f} ATL) is significantly above your fitness base ({s.ctl:.0f} CTL). A rest day is the right call.",
    }

    risk_map = {
        "Fresh":              None,
        "Ready":              None,
        "Slight Fatigue":     None,
        "Productive Fatigue": "low",
        "Heavy Load":         "moderate",
        "Very Fatigued":      "high",
        "Deep Fatigue":       "high",
    }

    # --- Insights ---
    insights = []

    # Volume insights
    if s.rolling_7d_hours > 10.0:
        insights.append(f"You've put in {s.rolling_7d_hours:.1f}h over the last 7 days — a big block of work.")
    elif s.rolling_7d_hours > 7.0:
        insights.append(f"Solid 7 days — {s.rolling_7d_hours:.1f}h of riding in the bank.")

    # Fitness base context
    if s.ctl < 40 and s.atl > 55:
        insights.append("Fitness is still building — heavier loads carry a bit more weight at this stage.")

    # Calorie deficit
    if s.calorie_balance < -600 and s.daily_tss > 60:
        insights.append("Calorie intake looks low for a training day — worth fuelling a bit more around sessions.")

    # --- Training profile context (deterministic, from stored data only) ---
    goal = p.get("goal", "")
    goal_custom = p.get("goal_custom", "")
    target_event_date = p.get("target_event_date", "")
    weekly_hours_target = float(p.get("weekly_hours_target") or 0)
    effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal

    if effective_goal == "Build aerobic base":
        if classification in ("Fresh", "Ready", "Slight Fatigue", "Productive Fatigue"):
            insights.append("Goal: aerobic base — good conditions for an endurance session today.")
        elif classification in ("Heavy Load", "Very Fatigued"):
            insights.append("Goal: aerobic base — fatigue is elevated. A lighter session will still contribute to your base.")

    elif effective_goal == "Raise FTP / threshold":
        if classification in ("Fresh", "Ready"):
            insights.append("Goal: raise FTP — you're in good shape for quality threshold work today.")
        elif classification in ("Productive Fatigue", "Slight Fatigue"):
            insights.append("Goal: raise FTP — quality work is still possible, but manage the effort level.")
        elif classification in ("Heavy Load", "Very Fatigued", "Deep Fatigue"):
            insights.append("Goal: raise FTP — legs need a bit more time to recover before quality work really pays off.")

    elif effective_goal == "Improve endurance for longer rides":
        if classification in ("Fresh", "Ready", "Slight Fatigue"):
            insights.append("Goal: longer rides — today is a good day for extended aerobic work.")
        elif classification in ("Heavy Load", "Very Fatigued"):
            insights.append("Goal: longer rides — fatigue is elevated. A lighter day will leave you better placed for a long ride soon.")

    elif effective_goal == "Prepare for an event":
        if target_event_date:
            try:
                days_to_event = (_date.fromisoformat(target_event_date) - _date.today()).days
                if days_to_event < 0:
                    pass  # event passed
                elif days_to_event <= 3:
                    insights.append(f"Event in {days_to_event} day(s) — keep the legs easy and save your energy.")
                elif days_to_event <= 7:
                    insights.append(f"Event in {days_to_event} days — taper week. Short and sharp if you ride at all.")
                elif days_to_event <= 14:
                    insights.append(f"Event in {days_to_event} days — start easing back on volume. Keep a bit of intensity.")
                else:
                    insights.append(f"Event in {days_to_event} days — keep building.")
            except ValueError:
                pass

    elif effective_goal == "Weight loss while maintaining performance":
        if s.calorie_balance < -400 and classification in ("Heavy Load", "Very Fatigued", "Deep Fatigue"):
            insights.append("High load and a calorie deficit — make sure to fuel harder sessions properly.")

    # Weekly hours tracking vs target
    if weekly_hours_target > 0 and s.rolling_7d_hours < weekly_hours_target * 0.5:
        insights.append(
            f"7-day hours ({s.rolling_7d_hours:.1f}h) are below your weekly target ({weekly_hours_target:.0f}h) — "
            "a quieter week, but it still counts."
        )

    # --- Planned workout context ---
    if pw:
        pw_type = pw.get("type", "")
        pw_title = pw.get("title", "planned session")
        if pw_type in _HARD_SESSION_TYPES:
            if classification in ("Very Fatigued", "Deep Fatigue"):
                insights.append(
                    f"Planned '{pw_title}' — legs are carrying a lot right now. "
                    "Backing off the intensity or shifting it a day would help you get more from it."
                )
            elif classification in ("Fresh", "Ready"):
                insights.append(f"Planned '{pw_title}' — readiness looks good for it.")
            elif classification in ("Heavy Load", "Productive Fatigue"):
                insights.append(
                    f"Planned '{pw_title}' — you're carrying some load. Listen to your body and adjust if needed."
                )
        elif pw_type == "endurance":
            if classification in ("Productive Fatigue", "Heavy Load"):
                insights.append(f"Planned '{pw_title}' — a steady aerobic ride is a good fit for where you are right now.")
            elif classification in ("Fresh", "Ready"):
                insights.append(f"Planned '{pw_title}' — well matched to today.")
        elif pw_type in _EASY_SESSION_TYPES:
            if classification in ("Very Fatigued", "Deep Fatigue", "Heavy Load"):
                insights.append(f"Planned '{pw_title}' — exactly the right call today.")
            elif classification in ("Fresh", "Ready"):
                insights.append(
                    f"Planned '{pw_title}' — you're fresh, but an easy session helps consolidate recent work."
                )

    return CoachingResult(
        classification=classification,
        recommendation=recommendation,
        readiness_score=readiness,
        headline=headlines[classification],
        explanation=explanations[classification],
        risk_flag=risk_map[classification],
        flags=flags,
        insights=insights[:5],
        ctl=s.ctl,
        atl=s.atl,
        tsb=s.tsb,
        fatigue_ratio=ratio,
        tss_today=s.daily_tss,
    )
