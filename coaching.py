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
        return "Fresh", "Hard session or test appropriate"
    elif tsb >= 0:
        return "Ready", "Normal training appropriate"
    elif tsb >= -10:
        return "Slight Fatigue", "Normal training still fine"
    elif tsb >= -25:
        return "Productive Fatigue", "Moderate to hard training may still be appropriate"
    elif tsb >= -40:
        return "Heavy Load", "Endurance or controlled session recommended"
    elif tsb >= -60:
        return "Very Fatigued", "Recovery or easy endurance recommended"
    else:
        return "Deep Fatigue", "Rest strongly recommended"


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
        flags.append(f"Load spike: fatigue ratio {ratio:.2f} — recent load is {ratio:.1f}× your fitness base.")
    elif ratio > 1.5:
        flags.append(f"Elevated load relative to fitness: ratio {ratio:.2f}.")

    # --- Headlines and explanations ---
    headlines = {
        "Fresh":              "Well rested — good window for a hard session or test.",
        "Ready":              "Ready to train — normal training load is appropriate.",
        "Slight Fatigue":     "Slight fatigue — training is still fine and productive.",
        "Productive Fatigue": "Carrying productive load — moderate to hard sessions are appropriate.",
        "Heavy Load":         "Significant load accumulated — endurance or controlled effort recommended.",
        "Very Fatigued":      "High fatigue — easy ride or full recovery session recommended.",
        "Deep Fatigue":       "Deep fatigue — rest is strongly recommended today.",
    }

    explanations = {
        "Fresh":              f"TSB is {s.tsb:+.0f}. CTL {s.ctl:.0f} is well expressed — you are fresh and ready.",
        "Ready":              f"TSB is {s.tsb:+.0f}. Fitness (CTL {s.ctl:.0f}) and load (ATL {s.atl:.0f}) are well balanced.",
        "Slight Fatigue":     f"TSB is {s.tsb:+.0f}. Small fatigue deficit — this is a normal training state.",
        "Productive Fatigue": f"TSB is {s.tsb:+.0f}. You are in a productive loading phase (ATL {s.atl:.0f} vs CTL {s.ctl:.0f}).",
        "Heavy Load":         f"TSB is {s.tsb:+.0f}. Recent load (ATL {s.atl:.0f}) is building above your fitness base (CTL {s.ctl:.0f}).",
        "Very Fatigued":      f"TSB is {s.tsb:+.0f}. Load (ATL {s.atl:.0f}) substantially exceeds fitness (CTL {s.ctl:.0f}). Time to recover.",
        "Deep Fatigue":       f"TSB is {s.tsb:+.0f}. Fatigue (ATL {s.atl:.0f}) far exceeds fitness (CTL {s.ctl:.0f}). Rest today.",
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
        insights.append(f"Big week: {s.rolling_7d_hours:.1f}h in the last 7 days.")
    elif s.rolling_7d_hours > 7.0:
        insights.append(f"Solid week: {s.rolling_7d_hours:.1f}h in the last 7 days.")

    # Fitness base context
    if s.ctl < 40 and s.atl > 55:
        insights.append("Fitness base is still building — heavy loads have more impact at this stage.")

    # Calorie deficit
    if s.calorie_balance < -600 and s.daily_tss > 60:
        insights.append("Significant calorie deficit on a training day — consider improving fuelling.")

    # --- Training profile context (deterministic, from stored data only) ---
    goal = p.get("goal", "")
    goal_custom = p.get("goal_custom", "")
    target_event_date = p.get("target_event_date", "")
    weekly_hours_target = float(p.get("weekly_hours_target") or 0)
    effective_goal = goal_custom.strip() if goal == "Custom" and goal_custom.strip() else goal

    if effective_goal == "Build aerobic base":
        if classification in ("Fresh", "Ready", "Slight Fatigue", "Productive Fatigue"):
            insights.append("Goal: aerobic base — conditions are good for an endurance session today.")
        elif classification in ("Heavy Load", "Very Fatigued"):
            insights.append("Goal: aerobic base — fatigue is elevated. A lighter session today will still build aerobic fitness.")

    elif effective_goal == "Raise FTP / threshold":
        if classification in ("Fresh", "Ready"):
            insights.append("Goal: raise FTP — readiness is good for quality threshold work.")
        elif classification in ("Productive Fatigue", "Slight Fatigue"):
            insights.append("Goal: raise FTP — you can still do quality work, but manage the effort level.")
        elif classification in ("Heavy Load", "Very Fatigued", "Deep Fatigue"):
            insights.append("Goal: raise FTP — fatigue is too high for quality threshold work. Prioritise recovery first.")

    elif effective_goal == "Improve endurance for longer rides":
        if classification in ("Fresh", "Ready", "Slight Fatigue"):
            insights.append("Goal: longer ride endurance — today is a good day for extended aerobic work.")
        elif classification in ("Heavy Load", "Very Fatigued"):
            insights.append("Goal: longer ride endurance — too fatigued for a long effort today. Rest and come back fresh.")

    elif effective_goal == "Prepare for an event":
        if target_event_date:
            try:
                days_to_event = (_date.fromisoformat(target_event_date) - _date.today()).days
                if days_to_event < 0:
                    pass  # event passed, ignore
                elif days_to_event <= 3:
                    insights.append(f"Event in {days_to_event} day(s) — keep legs easy and save your energy.")
                elif days_to_event <= 7:
                    insights.append(f"Event in {days_to_event} days — taper is in effect. Short, sharp efforts only.")
                elif days_to_event <= 14:
                    insights.append(f"Event in {days_to_event} days — begin reducing volume. Maintain some intensity.")
                else:
                    insights.append(f"Event in {days_to_event} days — continue building.")
            except ValueError:
                pass

    elif effective_goal == "Weight loss while maintaining performance":
        if s.calorie_balance < -400 and classification in ("Heavy Load", "Very Fatigued", "Deep Fatigue"):
            insights.append("Calorie deficit combined with high training load — fuel harder sessions properly.")

    # Weekly hours tracking vs target
    if weekly_hours_target > 0 and s.rolling_7d_hours < weekly_hours_target * 0.5:
        insights.append(
            f"Rolling 7-day hours ({s.rolling_7d_hours:.1f}h) are well below target ({weekly_hours_target:.0f}h)."
        )

    # --- Planned workout context ---
    if pw:
        pw_type = pw.get("type", "")
        pw_title = pw.get("title", "planned session")
        if pw_type in _HARD_SESSION_TYPES:
            if classification in ("Very Fatigued", "Deep Fatigue"):
                insights.append(
                    f"Planned '{pw_title}' is a hard session — readiness is low. "
                    "Consider reducing intensity or moving it by a day."
                )
            elif classification in ("Fresh", "Ready"):
                insights.append(f"Planned '{pw_title}' — readiness is good. Well placed.")
            elif classification in ("Heavy Load", "Productive Fatigue"):
                insights.append(
                    f"Planned '{pw_title}' — you are carrying some load. Proceed carefully and listen to your body."
                )
        elif pw_type == "endurance":
            if classification in ("Productive Fatigue", "Heavy Load"):
                insights.append(f"Planned '{pw_title}' (endurance) — a steady aerobic ride is a good fit right now.")
            elif classification in ("Fresh", "Ready"):
                insights.append(f"Planned '{pw_title}' — looks well matched to where you are today.")
        elif pw_type in _EASY_SESSION_TYPES:
            if classification in ("Very Fatigued", "Deep Fatigue", "Heavy Load"):
                insights.append(f"Planned '{pw_title}' — exactly the right call given current fatigue.")
            elif classification in ("Fresh", "Ready"):
                insights.append(
                    f"Planned '{pw_title}' — you are fresh, but easy sessions help consolidate recent training."
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
