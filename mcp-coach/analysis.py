"""
analysis.py — Coaching intelligence layer for mcp-coach.

Takes pre-fetched training data and produces athlete-specific coaching signals:
  - Season / training phase
  - Decision engine: priority_of_the_week
  - Race fitness interpretation
  - Hill climb readiness
  - CTL trajectory
  - Peak form framing

No DB access here — pure computation on already-fetched data.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional
from resources import ATHLETE_CONTEXT


# ── Season ────────────────────────────────────────────────────────────────────

def get_season(today: Optional[date] = None) -> str:
    """Return the current training season phase."""
    if today is None:
        today = date.today()
    m = today.month
    if m in [3, 4, 5]:
        return "spring_build"
    if m in [6, 7, 8]:
        return "summer_threshold"
    if m in [9, 10]:
        return "autumn_sharpening"
    return "winter_base"


def get_season_context(today: Optional[date] = None) -> str:
    """Return a one-line season coaching frame."""
    season = get_season(today)
    return ATHLETE_CONTEXT["seasonal_framework"].get(season, "")


# ── CTL interpretation ────────────────────────────────────────────────────────

def interpret_ctl(ctl: float) -> dict:
    """
    Map CTL to athlete-specific context and race fitness status.
    Returns dict with 'context' and 'race_fitness' strings.
    """
    for ref in ATHLETE_CONTEXT["ctl_reference"]:
        lo, hi = ref["ctl_range"]
        if lo <= ctl < hi:
            return {"context": ref["context"], "race_fitness": ref.get("race_fitness", "")}
    if ctl >= 75:
        return {"context": "above historical peak load — exceptional form", "race_fitness": "peak form"}
    return {"context": "very early base — weeks away from training meaningfully", "race_fitness": "rebuilding"}


def interpret_ctl_trend(ctl: float, prev_ctl: float) -> str:
    """
    Interpret the 7-day CTL trend.
    prev_ctl = CTL 7 days ago.
    """
    if prev_ctl <= 0:
        return ""
    delta = ctl - prev_ctl
    rate_per_week = delta  # CTL delta over 7 days
    if rate_per_week > 3:
        return f"CTL rising fast (+{delta:.1f} in 7d) — ensure you're absorbing the load, not just accumulating it."
    if rate_per_week > 1:
        return f"CTL building progressively (+{delta:.1f} in 7d) — good trajectory."
    if rate_per_week > -1:
        return "CTL holding steady — consistent maintenance phase."
    if rate_per_week > -3:
        return f"CTL trending down ({delta:.1f} in 7d) — lighter week or planned recovery."
    return f"CTL dropping ({delta:.1f} in 7d) — significant unloading period. Check if intentional."


# ── Load interpretation ───────────────────────────────────────────────────────

def interpret_load(ctl: float, atl: float, tsb: float) -> str:
    """
    Plain-language load state. Avoids raw metric recitation.
    Grounded in what this specific athlete responds well to.
    """
    ratio = atl / ctl if ctl > 0 else 1.0

    if tsb > 10:
        return "Fresh legs — positive form balance, good day for quality or a hard effort."
    if tsb > 0:
        return "Slightly fresh — small positive balance. Solid for a quality session."
    if tsb >= -10:
        return "Light fatigue — normal mid-week carry. Threshold work is productive here."
    if tsb >= -20:
        return "Moderate fatigue — mid-block territory. One more quality session is appropriate; don't stack a third consecutive hard day."
    if tsb >= -30:
        if ratio < 1.5:
            return "Meaningful fatigue accumulated — typical for a solid build block. Hard sessions are still productive but choose one."
        return "Heavy block fatigue — ATL well above CTL. Protect the next session's quality; back off volume today."
    if tsb >= -45:
        return "Significant fatigue carrying over — honest assessment of what the body can deliver matters. A reduced session still adapts."
    return "Very heavy fatigue load — recovery priority. Forcing a hard session here risks a flat or injurious week."


# ── Race fitness and hill climb ───────────────────────────────────────────────

def interpret_race_fitness(ctl: float, ftp: float, tsb: float) -> str:
    """
    Interpret readiness for race-level performance.
    Diesel engine: threshold fitness and aerobic durability matter most.
    """
    ctl_data = interpret_ctl(ctl)
    race_fit = ctl_data["race_fitness"]

    best_ftp = max(p["ftp_w"] for p in ATHLETE_CONTEXT["historical_ftp"]["peaks"])
    ftp_pct = round(ftp / best_ftp * 100) if best_ftp else 0

    if race_fit == "race fit" and ftp_pct >= 90 and tsb > -15:
        return (
            f"Race-fit condition — FTP at {ftp_pct}% of historical best, "
            f"fitness base in peak form range, form balance positive. "
            f"Ready to race or test."
        )
    if race_fit in ("race fit", "approaching race fit"):
        form_note = "form positive" if tsb > 0 else f"carrying {abs(tsb):.0f} TSB points of fatigue"
        return (
            f"Approaching race fitness — FTP at {ftp_pct}% of best, "
            f"{ctl_data['context']}, {form_note}. "
            f"A short taper would produce a good day."
        )
    if race_fit == "building":
        return (
            f"Building toward race fitness — FTP at {ftp_pct}% of best, "
            f"aerobic base developing. Another 4–8 weeks of consistent load "
            f"would bring this to a competitive level."
        )
    return (
        f"Early stage comeback — FTP at {ftp_pct}% of best. "
        f"Race fitness is a future target, not today's metric. "
        f"Focus on building the base consistently."
    )


def interpret_hill_climb_readiness(ctl: float, ftp: float, tsb: float) -> str:
    """
    Hill climb TT specific readiness interpretation.
    This athlete has won local hill climb TTs — it's a genuine priority event type.
    Key: FTP relative to peak, form balance, and sustained threshold capability.
    """
    best_ftp = max(p["ftp_w"] for p in ATHLETE_CONTEXT["historical_ftp"]["peaks"])
    ftp_pct = round(ftp / best_ftp * 100) if best_ftp else 0

    # Hill climbs are ~5–20min, need high FTP and positive form
    if ftp_pct >= 90 and tsb > -5:
        return f"Hill climb ready — FTP at {ftp_pct}% of best, form balance positive. Peak condition for a TT effort."
    if ftp_pct >= 85 and tsb > -15:
        return f"Good hill climb shape — FTP at {ftp_pct}% of best. A 3–5 day taper would sharpen the effort."
    if ftp_pct >= 75:
        return f"Developing hill climb fitness — FTP at {ftp_pct}% of best. Threshold work over the next month will convert to TT performance."
    return f"Hill climb fitness is a future target — FTP at {ftp_pct}% of best. Building the engine first."


# ── FTP context ───────────────────────────────────────────────────────────────

def interpret_ftp(current_ftp: float) -> str:
    """Contextualise current FTP against athlete's verified historical peaks."""
    peaks = ATHLETE_CONTEXT["historical_ftp"]["peaks"]
    best = max(p["ftp_w"] for p in peaks)
    target_lo, target_hi = ATHLETE_CONTEXT["historical_ftp"]["current_target_w"]
    pct = round(current_ftp / best * 100)

    if current_ftp >= 270:
        return f"{current_ftp:.0f}W — within historical peak range ({best}W best). Strong form."
    if current_ftp >= target_hi:
        return f"{current_ftp:.0f}W — above initial comeback target ({target_lo}–{target_hi}W). Progression on track, push the ceiling."
    if current_ftp >= target_lo:
        return f"{current_ftp:.0f}W — within comeback target ({target_lo}–{target_hi}W). Tracking well."
    gap = target_lo - current_ftp
    return (
        f"{current_ftp:.0f}W — {gap:.0f}W below comeback target ({target_lo}–{target_hi}W), "
        f"at {pct}% of historical best ({best}W)."
    )


# ── Decision engine ───────────────────────────────────────────────────────────

def compute_priority_of_week(
    ctl: float,
    atl: float,
    tsb: float,
    day_of_week: int,       # Mon=0, Sun=6
    weekly_tss_so_far: float,
    weekly_tss_target: float,
    season: str,
    weekly_rides_so_far: int,
    recent_ride_tss: list,  # TSS of last 3 rides, most recent first
) -> str:
    """
    Decision engine: single authoritative statement of this week's training priority.

    This is the coaching judgement call that orients all other advice.
    Chain gang Tuesday is always the week's anchor session — everything else serves it.
    """
    ratio = atl / ctl if ctl > 0 else 1.0
    target_pct = (weekly_tss_so_far / weekly_tss_target * 100) if weekly_tss_target > 0 else 50

    # Monday: chain gang is tomorrow
    if day_of_week == 0:
        if tsb < -20:
            return (
                "Heavy fatigue going into chain gang week — keep today easy, "
                "arrive at Tuesday's chain gang with whatever legs are available. "
                "Don't try to push through the fatigue today."
            )
        return (
            "Chain gang tomorrow — protect today. Easy spin or rest. "
            "The week's quality anchor is Tuesday; save it."
        )

    # Tuesday: chain gang day
    if day_of_week == 1:
        if tsb > 5:
            return "Chain gang today — legs are fresh, target the front group and hold it."
        if tsb > -15:
            return "Chain gang today — moderate fatigue carry, but threshold range is productive. Ride to effort, not to a number."
        return "Chain gang today with heavy legs — ride at sustainable threshold, don't bury yourself. The fitness is in the completion, not the heroics."

    # Wednesday: recovery day after chain gang
    if day_of_week == 2:
        last_tss = recent_ride_tss[0] if recent_ride_tss else 0
        if last_tss > 150:
            return (
                "Day after a big chain gang effort — active recovery only today. "
                "The adaptation happens in the rest, not the next session."
            )
        return (
            "Post-chain gang Wednesday — lighter session. "
            "Aerobic spin or rest. Let Tuesday do its job."
        )

    # Thursday-Friday: support session window
    if day_of_week in [3, 4]:
        if tsb > -20 and season in ("spring_build", "summer_threshold"):
            return (
                "Good day for a tempo or aerobic ride — chain gang is done, "
                "weekend is coming. Keep it controlled. Don't turn this into a third hard day."
            )
        if target_pct < 50:
            return (
                "Week is light on load — a solid aerobic ride today builds the week "
                "without burying the legs before the weekend."
            )
        return "Mid-week support — aerobic work if the legs cooperate, spin or rest if not."

    # Weekend (Saturday-Sunday): long ride territory
    if day_of_week in [5, 6]:
        if ctl < 45:
            return (
                "Weekend long ride is the priority — aerobic durability is the key "
                "lever at this fitness level. 3+ hours at endurance pace compounds quickly."
            )
        if target_pct < 60:
            return (
                "Big day available — this is where the weekly TSS accumulates. "
                "Long aerobic ride, controlled effort, quality kilometres."
            )
        if target_pct > 90:
            return (
                "Weekly load target nearly met — the long ride can be moderate length. "
                "Quality over adding bulk; the adaptation is already earned."
            )
        return "Weekend long ride — aerobic priority. Consistent, controlled, purposeful."

    return "Consistent aerobic work — each session supports the chain gang and weekly load target."


# ── Minimum context object ────────────────────────────────────────────────────

def build_coaching_intelligence(
    ctl: float,
    atl: float,
    tsb: float,
    prev_ctl: float,
    ftp: float,
    day_of_week: int,
    weekly_tss: float,
    weekly_tss_target: float,
    weekly_rides: int,
    recent_rides: list,
    today_rides: list,
    season: Optional[str] = None,
) -> dict:
    """
    Produce the minimum coaching intelligence context object.
    All interpretation done here — Groq only receives pre-interpreted language.

    Returns a dict consumed by _build_narrative_brief() in prompts.py.
    """
    if season is None:
        season = get_season()

    recent_tss_list = [float(r.get("tss") or 0) for r in recent_rides[:3]]
    last_ride = recent_rides[0] if recent_rides else None

    # Core interpretations
    ctl_interp    = interpret_ctl(ctl)
    ctl_trend     = interpret_ctl_trend(ctl, prev_ctl)
    load_state    = interpret_load(ctl, atl, tsb)
    ftp_context   = interpret_ftp(ftp)
    season_ctx    = get_season_context()
    race_fitness  = interpret_race_fitness(ctl, ftp, tsb)
    hill_readiness = interpret_hill_climb_readiness(ctl, ftp, tsb)

    priority = compute_priority_of_week(
        ctl=ctl, atl=atl, tsb=tsb,
        day_of_week=day_of_week,
        weekly_tss_so_far=weekly_tss,
        weekly_tss_target=weekly_tss_target,
        season=season,
        weekly_rides_so_far=weekly_rides,
        recent_ride_tss=recent_tss_list,
    )

    # Last ride assessment
    last_ride_assessment = None
    if last_ride:
        tss = float(last_ride.get("tss") or 0)
        mins = int(float(last_ride.get("moving_time_s") or 0) / 60)
        name = last_ride.get("name", "Ride")
        if tss > 180:
            last_ride_assessment = f"Big session — {tss:.0f} TSS over {mins}min. That's race-level stimulus."
        elif tss > 120:
            last_ride_assessment = f"Quality session — {tss:.0f} TSS, {mins}min. Meaningful threshold stimulus."
        elif tss > 60:
            last_ride_assessment = f"Solid aerobic session — {tss:.0f} TSS, {mins}min. Good base work."
        elif tss > 0:
            last_ride_assessment = f"Light session — {tss:.0f} TSS, {mins}min. Recovery or easy spin."

    return {
        "season":             season,
        "season_context":     season_ctx,
        "ctl_context":        ctl_interp["context"],
        "race_fitness":       ctl_interp["race_fitness"],
        "ctl_trend":          ctl_trend,
        "load_state":         load_state,
        "ftp_context":        ftp_context,
        "race_fitness_interp": race_fitness,
        "hill_climb_readiness": hill_readiness,
        "priority_of_week":   priority,
        "last_ride_assessment": last_ride_assessment,
    }
