"""
coaching.py - Deterministic TSB-based coaching engine.
TSB zones drive classification. Fatigue ratio provides supplementary flags only.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


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
    def from_metrics(cls, m: dict, nutrition_balance=0.0, calories_today=0.0):
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


def generate_weekly_outlook(
    state: TrainingState,
    goal: str = "",
    long_ride_day: str = "",
) -> dict:
    """
    Deterministic weekly training outlook from training state + athlete goal.
    Stored in coaching_cache every sync. No LLM involved.
    """
    goal_lower = (goal or "").lower()
    high_load    = state.tsb < -20
    very_fatigued = state.tsb < -35

    # ── Focus, session types, and coaching note keyed to goal ────────────────
    if any(x in goal_lower for x in ["ftp", "threshold"]):
        focus = "Threshold build" if not high_load else "Endurance + load absorption"
        key_sessions = ["Threshold or hard group ride"]
        secondary_sessions = ["Aerobic endurance ride"]
        if very_fatigued:
            coaching_note = (
                "High fatigue from recent load — keep the hard session short and "
                "controlled, then let the adaptation land before the next block."
            )
        elif high_load:
            coaching_note = (
                "Fatigue is accumulating. One solid threshold effort, then back off "
                "and let the week's stress consolidate."
            )
        else:
            coaching_note = (
                "Good position to push threshold work. One quality hard session "
                "is the priority; support it with aerobic volume."
            )

    elif "crit" in goal_lower:
        focus = "Race-speed prep"
        key_sessions = ["Hard group ride with race-pace efforts"]
        secondary_sessions = ["Short aerobic session"]
        coaching_note = (
            "Crit fitness needs repeated efforts above threshold. "
            "One hard session centred on race pace, supported by aerobic riding."
        )

    elif "road race" in goal_lower:
        focus = "Race endurance"
        key_sessions = ["Hard group ride or sustained race-pace effort"]
        secondary_sessions = ["Tempo or aerobic ride"]
        coaching_note = (
            "Road racing demands aerobic durability and sustained threshold capacity. "
            "One hard session plus consistent volume."
        )

    elif "10" in goal_lower and "tt" in goal_lower:
        focus = "10-mile TT prep"
        key_sessions = ["Sustained threshold effort"]
        secondary_sessions = ["Aerobic support ride"]
        coaching_note = (
            "10-mile TT performance comes from sustained threshold power. "
            "One quality effort near or at threshold this week."
        )

    elif "25" in goal_lower and "tt" in goal_lower:
        focus = "25-mile TT prep"
        key_sessions = ["Sustained threshold effort"]
        secondary_sessions = ["Aerobic endurance ride"]
        coaching_note = (
            "25-mile TT fitness is built on consistent threshold work and aerobic base. "
            "Prioritise the quality effort this week."
        )

    elif "50" in goal_lower and "tt" in goal_lower:
        focus = "50-mile TT prep"
        key_sessions = ["Long sustained effort"]
        secondary_sessions = ["Aerobic durability ride"]
        coaching_note = (
            "50-mile TT prep centres on aerobic durability with threshold capacity. "
            "Volume and sustained effort both matter this week."
        )

    elif "hill climb" in goal_lower:
        focus = "Climbing strength"
        key_sessions = ["Hard climbing session or sustained gradient effort"]
        secondary_sessions = ["Aerobic endurance ride"]
        coaching_note = (
            "Hill climb prep needs hard efforts on gradient. "
            "One quality climbing session is the week's centrepiece."
        )

    elif "gran fondo" in goal_lower:
        focus = "Aerobic durability"
        key_sessions = ["Long endurance ride"]
        secondary_sessions = ["Steady group ride"]
        coaching_note = (
            "Gran Fondo prep is primarily about aerobic volume. "
            "Get the long ride in and keep the week consistent."
        )

    elif any(x in goal_lower for x in ["sportive", "event prep"]):
        focus = "Event endurance"
        key_sessions = ["Long aerobic ride"]
        secondary_sessions = ["Steady group ride"]
        coaching_note = (
            "Sportive prep needs aerobic volume above all. "
            "The long ride is the key session; everything else is support."
        )

    elif "weight" in goal_lower:
        focus = "Active base"
        key_sessions = ["Steady endurance ride"]
        secondary_sessions = []
        coaching_note = (
            "Consistent moderate-intensity riding supports fitness and composition. "
            "Keep the week regular and well-fuelled."
        )

    else:
        focus = "Training consistency"
        key_sessions = ["Main weekly ride"]
        secondary_sessions = []
        coaching_note = "Keep training consistent. Regular aerobic work builds the foundation."

    # ── Long ride from preferred day ─────────────────────────────────────────
    endurance_sessions = [f"{long_ride_day} — Long ride"] if long_ride_day else []

    return {
        "focus":              focus,
        "key_sessions":       key_sessions,
        "secondary_sessions": secondary_sessions,
        "endurance_sessions": endurance_sessions,
        "coaching_note":      coaching_note,
    }


STATUS_COLOR = {
    "Fresh": "#4caf50",
    "Balanced": "#26c6da",
    "Fatigued": "#ffb300",
    "Very Fatigued": "#ff5722",
    "Overreaching": "#f44336",
}


def classify_tsb(tsb):
    if tsb >= 10:
        return "Fresh", "Hard training is appropriate"
    elif tsb >= -10:
        return "Balanced", "Normal training load"
    elif tsb >= -30:
        return "Fatigued", "Moderate session"
    elif tsb >= -60:
        return "Very Fatigued", "Easy ride or recovery"
    else:
        return "Overreaching", "Rest day recommended"


def readiness_from_tsb(tsb):
    return round(max(0.0, min(100.0, ((tsb + 60) / 80.0) * 100.0)), 1)


def readiness_label(score):
    if score >= 80:
        return "Fresh"
    if score >= 60:
        return "Good"
    if score >= 40:
        return "Moderate fatigue"
    if score >= 20:
        return "Heavy fatigue"
    return "Deep fatigue"


def evaluate(s: TrainingState) -> CoachingResult:
    classification, recommendation = classify_tsb(s.tsb)
    readiness = readiness_from_tsb(s.tsb)
    ratio = s.fatigue_ratio
    flags = []
    if ratio > 2.0:
        flags.append(f"Load spike risk: fatigue ratio {ratio:.2f}")
    elif ratio > 1.5:
        flags.append(f"High load relative to fitness: ratio {ratio:.2f}")

    headlines = {
        "Fresh": "Well-rested — hard training is appropriate today.",
        "Balanced": "Normal training load is appropriate today.",
        "Fatigued": "Moderate session recommended — manage the fatigue.",
        "Very Fatigued": "Easy ride or recovery day recommended.",
        "Overreaching": "Rest day recommended — significant fatigue accumulated.",
    }

    explanations = {
        "Fresh": f"TSB is {s.tsb:+.0f}. Fitness (CTL {s.ctl:.0f}) is well expressed. Good window for a quality session.",
        "Balanced": f"TSB is {s.tsb:+.0f}. Fitness (CTL {s.ctl:.0f}) and load (ATL {s.atl:.0f}) are matched. Normal training fits.",
        "Fatigued": f"TSB is {s.tsb:+.0f}. Recent load (ATL {s.atl:.0f}) is above fitness base (CTL {s.ctl:.0f}). Keep it moderate.",
        "Very Fatigued": f"TSB is {s.tsb:+.0f}. Load (ATL {s.atl:.0f}) substantially exceeds fitness (CTL {s.ctl:.0f}). Easy or rest.",
        "Overreaching": f"TSB is {s.tsb:+.0f}. Fatigue (ATL {s.atl:.0f}) far exceeds fitness (CTL {s.ctl:.0f}). Rest recommended.",
    }

    risk_map = {
        "Fresh": None,
        "Balanced": None,
        "Fatigued": "low",
        "Very Fatigued": "moderate",
        "Overreaching": "high",
    }

    insights = []
    if ratio > 2.0:
        insights.append(f"Recent load (ATL {s.atl:.0f}) is {ratio:.1f}x fitness base (CTL {s.ctl:.0f}).")
    if s.ctl < 40 and s.atl > 55:
        insights.append("Fitness base rebuilding — heavy loads land harder at this stage.")
    if s.rolling_7d_hours > 8.0:
        insights.append(f"Weekly volume: {s.rolling_7d_hours:.1f}h — substantial.")
    if s.calorie_balance < -600 and s.daily_tss > 60:
        insights.append("Significant calorie deficit on a hard training day.")

    return CoachingResult(
        classification=classification,
        recommendation=recommendation,
        readiness_score=readiness,
        headline=headlines[classification],
        explanation=explanations[classification],
        risk_flag=risk_map[classification],
        flags=flags,
        insights=insights[:4],
        ctl=s.ctl,
        atl=s.atl,
        tsb=s.tsb,
        fatigue_ratio=ratio,
        tss_today=s.daily_tss,
    )
