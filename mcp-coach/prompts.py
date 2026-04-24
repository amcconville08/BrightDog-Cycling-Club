"""
prompts.py — Context assembly and prompt architecture for mcp-coach.

Architecture:
  1. tools.py fetches raw data from club.db (source of truth)
  2. build_context() assembles and interprets everything into a structured packet
  3. _build_narrative_brief() converts the packet into a human-readable coaching brief
  4. build_system_prompt() injects athlete identity into the coach persona
  5. build_user_message() sends the brief + question to Groq

Groq's job: translate the pre-interpreted brief into natural coaching language.
Groq must NOT invent advice, compute metrics, or override the athlete-specific context.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional
from tools import (
    get_readiness_summary, get_recent_rides, get_today_rides,
    get_previous_week_rides, get_previous_week_summary,
    get_week_summary, get_training_goal, get_current_zones,
    estimate_ftp_candidate, get_athlete_profile, get_weekly_outlook,
)
from resources import ATHLETE_CONTEXT


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ride_summary(r: dict) -> str:
    """One-line readable ride summary."""
    mins = int(float(r.get("moving_time_s") or 0) / 60)
    tss  = float(r.get("tss") or 0)
    np   = r.get("np_watts")
    elev = r.get("elevation_m")
    parts = [f"{r['name']}", f"{mins}min", f"TSS {tss:.0f}"]
    if np and float(np) > 0:
        parts.append(f"NP {float(np):.0f}W")
    if elev and float(elev) > 50:
        parts.append(f"+{float(elev):.0f}m")
    return ", ".join(parts)


def _interpret_ctl(ctl: float) -> str:
    """Map CTL to athlete-specific context from reference points."""
    for ref in ATHLETE_CONTEXT["ctl_reference"]:
        lo, hi = ref["ctl_range"]
        if lo <= ctl <= hi:
            return ref["context"]
    if ctl > 65:
        return "very high fitness load — above historical peaks"
    return "early base — well below race-fit level"


def _interpret_load(ctl: float, atl: float, tsb: float) -> str:
    """
    Produce a plain-language load interpretation grounded in athlete context.
    Avoids raw metric recitation — gives meaning instead.
    """
    if tsb > 10:
        return "Fresh legs — positive TSB, good time for quality or a harder effort."
    if tsb > 0:
        return "Slightly fresh — small positive TSB, solid day for a quality session."
    if tsb >= -15:
        return "Light fatigue carry — normal mid-week territory, threshold work is productive here."
    if tsb >= -30:
        ratio = atl / ctl if ctl > 0 else 1
        if ratio < 1.4:
            return "Meaningful fatigue accumulated — typical for an active build block. Hard sessions are still appropriate today."
        return "Deep in a load block — fatigue is real but expected. One more hard session is fine; don't stack a third consecutive hard day."
    if tsb >= -45:
        return "Heavy fatigue carrying over — best to be honest about what the body can deliver today. A reduced session still has value."
    return "Significant accumulated load — recovery priority. Forcing a hard session here risks a flat or injurious week."


def _ftp_vs_history(current_ftp: float) -> str:
    """Contextualise current FTP against athlete's verified historical peaks."""
    peaks = ATHLETE_CONTEXT["historical_ftp"]["peaks"]
    best  = max(p["ftp_w"] for p in peaks)
    target_lo, target_hi = ATHLETE_CONTEXT["historical_ftp"]["current_target_w"]
    pct = round(current_ftp / best * 100)
    if current_ftp >= 270:
        return f"{current_ftp:.0f}W — within historical peak range ({best}W best). Strong form."
    if current_ftp >= target_hi:
        return f"{current_ftp:.0f}W — above current comeback target ({target_lo}–{target_hi}W). Progression on track."
    if current_ftp >= target_lo:
        return f"{current_ftp:.0f}W — within comeback target range ({target_lo}–{target_hi}W). Tracking well."
    gap = target_lo - current_ftp
    return (
        f"{current_ftp:.0f}W — {gap:.0f}W below comeback target ({target_lo}–{target_hi}W). "
        f"Currently at {pct}% of historical best ({best}W)."
    )


# ── Context assembly ──────────────────────────────────────────────────────────

def build_context(db_path: str, user_id: int) -> dict:
    """
    Assemble the full grounded context packet.
    All numbers come from the DB — Groq only ever sees this output.
    """
    readiness      = get_readiness_summary(db_path, user_id)
    today_rides    = get_today_rides(db_path, user_id)
    recent_rides   = get_recent_rides(db_path, user_id, days=14)
    prev_week_rides = get_previous_week_rides(db_path, user_id)
    prev_week_sum  = get_previous_week_summary(db_path, user_id)
    week           = get_week_summary(db_path, user_id)
    goal           = get_training_goal(db_path, user_id)
    zones          = get_current_zones(db_path, user_id)
    profile        = get_athlete_profile(db_path, user_id)
    ftp_est        = estimate_ftp_candidate(db_path, user_id)
    weekly_outlook = get_weekly_outlook(db_path, user_id)

    today_str  = date.today().isoformat()
    today_day  = date.today().strftime("%A")
    day_num    = date.today().weekday()   # Mon=0
    days_left  = 6 - day_num
    is_monday  = (day_num == 0)

    ctl   = float(readiness.get("ctl", 0))
    atl   = float(readiness.get("atl", 0))
    tsb   = float(readiness.get("tsb", 0))
    ftp   = float(zones.get("ftp", 200))
    ratio = round(atl / ctl, 2) if ctl > 0 else 0

    # Pre-interpreted strings
    load_interp  = _interpret_load(ctl, atl, tsb)
    ctl_interp   = _interpret_ctl(ctl)
    ftp_interp   = _ftp_vs_history(ftp)

    # Today's ride(s)
    if today_rides:
        total_tss  = sum(float(r["tss"] or 0) for r in today_rides)
        total_mins = sum(int(float(r["moving_time_s"] or 0) / 60) for r in today_rides)
        today_ctx = {
            "completed":     True,
            "total_tss":     round(total_tss, 1),
            "total_minutes": total_mins,
            "rides": [
                {
                    "name":         r["name"],
                    "duration_min": int(float(r["moving_time_s"] or 0) / 60),
                    "distance_km":  round(float(r["distance_m"] or 0) / 1000, 1),
                    "tss":          round(float(r["tss"] or 0), 1),
                    "np_watts":     round(float(r["np_watts"]), 0) if r.get("np_watts") else None,
                    "elevation_m":  round(float(r["elevation_m"]), 0) if r.get("elevation_m") else None,
                }
                for r in today_rides
            ],
        }
    else:
        today_ctx = {"completed": False}

    # Recent rides (last 10, excluding today)
    recent = [
        {
            "date":  r["date"],
            "name":  r["name"],
            "mins":  int(float(r["moving_time_s"] or 0) / 60),
            "tss":   round(float(r["tss"] or 0), 1),
            "np":    round(float(r["np_watts"]), 0) if r.get("np_watts") else None,
            "elev":  round(float(r["elevation_m"]), 0) if r.get("elevation_m") else None,
        }
        for r in recent_rides if r["date"] != today_str
    ][:10]

    # Previous week rides (for review)
    prev_week = [
        {
            "date":  r["date"],
            "name":  r["name"],
            "mins":  int(float(r["moving_time_s"] or 0) / 60),
            "tss":   round(float(r["tss"] or 0), 1),
            "np":    round(float(r["np_watts"]), 0) if r.get("np_watts") else None,
            "elev":  round(float(r["elevation_m"]), 0) if r.get("elevation_m") else None,
        }
        for r in prev_week_rides
    ]

    # Week targets
    tss_target   = float(goal.get("weekly_tss_target") or 0)
    hours_target = float(goal.get("weekly_hours_target") or 0)
    week_tss_pct = round(float(week.get("weekly_tss", 0)) / tss_target * 100) if tss_target else None
    week_hrs_pct = round(float(week.get("weekly_hours", 0)) / hours_target * 100) if hours_target else None

    context_packet = {
        "date":     today_str,
        "day":      today_day,
        "is_monday": is_monday,
        "days_remaining_this_week": days_left,

        "athlete": {
            "name":          profile.get("name", "Athlete"),
            "ftp":           ftp,
            "ftp_context":   ftp_interp,
            "goal":          goal.get("goal") or goal.get("goal_custom") or "Not set",
            "target_event":  f"{goal.get('target_event_name','')} {goal.get('target_event_date','')}".strip() or None,
            "long_ride_day": goal.get("long_ride_day") or None,
        },

        "training_state": {
            "CTL":              round(ctl, 1),
            "ATL":              round(atl, 1),
            "TSB":              round(tsb, 1),
            "ratio_ATL_CTL":    ratio,
            "classification":   readiness.get("classification"),
            "readiness_score":  readiness.get("readiness"),
            "ctl_context":      ctl_interp,
            "load_interpretation": load_interp,
        },

        "today_ride": today_ctx,

        "pre_ride_suggestion": {
            "style":     readiness.get("ride_style"),
            "minutes":   readiness.get("suggested_duration_minutes"),
            "tss":       readiness.get("suggested_tss"),
            "rationale": readiness.get("ride_rationale"),
        },

        "week_so_far": {
            "rides":        week.get("weekly_rides", 0),
            "hours":        week.get("weekly_hours", 0),
            "tss":          round(float(week.get("weekly_tss", 0)), 0),
            "distance_km":  week.get("weekly_distance_km"),
            "tss_target":   tss_target or None,
            "hours_target": hours_target or None,
            "tss_pct":      week_tss_pct,
            "hours_pct":    week_hrs_pct,
        },

        "previous_week": {
            "summary": prev_week_sum,
            "rides":   prev_week,
        },

        "recent_rides": recent,

        "power_zones": {
            k: {"low": v[0], "high": v[1]}
            for k, v in zones.items()
            if k not in ("ftp", "source")
        },

        "ftp_estimate_from_rides": ftp_est,
        "weekly_outlook": weekly_outlook if weekly_outlook else None,
    }

    return {
        "context_packet": context_packet,
        "profile":        profile,
    }


# ── Narrative brief builder ───────────────────────────────────────────────────

def _build_narrative_brief(cp: dict) -> str:
    """
    Convert the structured context packet into a human-readable coaching brief.
    This is what Groq reads — pre-interpreted, athlete-specific, no raw JSON.
    """
    lines = []

    ts   = cp["training_state"]
    ath  = cp["athlete"]
    wk   = cp["week_so_far"]
    pw   = cp["previous_week"]
    wo   = cp.get("weekly_outlook") or {}
    sug  = cp.get("pre_ride_suggestion") or {}
    tod  = cp["today_ride"]
    rec  = cp.get("recent_rides") or []

    # ── Date and athlete ──────────────────────────────────────────────────────
    lines.append(f"TODAY: {cp['day']} {cp['date']}")
    lines.append(f"ATHLETE: {ath['name']} | FTP {ath['ftp_context']}")
    goal_str = ath.get("goal") or ""
    if goal_str and goal_str != "Not set":
        lines.append(f"GOAL: {goal_str}")
    if ath.get("target_event"):
        lines.append(f"TARGET EVENT: {ath['target_event']}")
    lines.append("")

    # ── Training state ────────────────────────────────────────────────────────
    lines.append("TRAINING STATE:")
    lines.append(f"  Fitness base (CTL {ts['CTL']:.0f}): {ts['ctl_context']}")
    lines.append(f"  Load & form: {ts['load_interpretation']}")
    lines.append(f"  Readiness: {ts.get('readiness_score', '—')}/100")
    lines.append("")

    # ── Today's ride ──────────────────────────────────────────────────────────
    if tod.get("completed"):
        lines.append("TODAY'S RIDING (completed):")
        for r in tod["rides"]:
            parts = [r["name"], f"{r['duration_min']}min", f"TSS {r['tss']}"]
            if r.get("np_watts"):
                parts.append(f"NP {r['np_watts']:.0f}W")
            if r.get("elevation_m") and r["elevation_m"] > 50:
                parts.append(f"+{r['elevation_m']:.0f}m")
            lines.append(f"  - {', '.join(parts)}")
    else:
        lines.append("TODAY'S RIDING: None yet.")
        if sug.get("style"):
            sug_line = f"  Suggestion: {sug['style']}"
            if sug.get("minutes"):
                sug_line += f", ~{sug['minutes']}min"
            if sug.get("tss"):
                sug_line += f", TSS ~{sug['tss']:.0f}"
            lines.append(sug_line)
            if sug.get("rationale"):
                lines.append(f"  Rationale: {sug['rationale']}")
    lines.append("")

    # ── Recent rides ──────────────────────────────────────────────────────────
    if rec:
        lines.append("RECENT RIDES (last 10 days):")
        for r in rec[:8]:
            parts = [r["date"], r["name"], f"{r['mins']}min", f"TSS {r['tss']}"]
            if r.get("np"):
                parts.append(f"NP {r['np']:.0f}W")
            if r.get("elev") and r["elev"] > 50:
                parts.append(f"+{r['elev']:.0f}m")
            lines.append(f"  {', '.join(parts)}")
        lines.append("")

    # ── This week so far ──────────────────────────────────────────────────────
    week_line = (
        f"THIS WEEK SO FAR: {wk['rides']} rides, "
        f"{wk['hours']}h, TSS {wk['tss']:.0f}"
    )
    if wk.get("tss_target"):
        pct = wk.get("tss_pct") or 0
        week_line += f" ({pct}% of {wk['tss_target']:.0f} TSS target)"
    lines.append(week_line)
    lines.append(f"Days remaining this week: {cp['days_remaining_this_week']}")
    lines.append("")

    # ── Previous week ─────────────────────────────────────────────────────────
    ps = pw.get("summary", {})
    if ps.get("rides"):
        lines.append(
            f"PREVIOUS WEEK: {ps['rides']} rides, {ps['hours']}h, "
            f"TSS {ps['tss']:.0f}, {ps['distance_km']}km"
        )
        for r in (pw.get("rides") or []):
            parts = [r["date"], r["name"], f"{r['mins']}min", f"TSS {r['tss']}"]
            if r.get("np"):
                parts.append(f"NP {r['np']:.0f}W")
            lines.append(f"  {', '.join(parts)}")
        lines.append("")

    # ── Weekly intent ─────────────────────────────────────────────────────────
    if wo.get("focus"):
        lines.append(f"WEEK INTENT: {wo['focus']}")
        if wo.get("key_sessions"):
            lines.append(f"  Key sessions: {'; '.join(wo['key_sessions'])}")
        if wo.get("coaching_note"):
            lines.append(f"  Coach note: {wo['coaching_note']}")
        lines.append("")

    # ── FTP estimate from recent rides ────────────────────────────────────────
    if cp.get("ftp_estimate_from_rides"):
        lines.append(f"CURRENT FTP ESTIMATE (from recent rides): {cp['ftp_estimate_from_rides']}W")
        lines.append("")

    return "\n".join(lines)


def _build_previous_week_brief(cp: dict) -> str:
    """
    Focused brief for the previous week — used in Monday weekly review generation.
    """
    pw   = cp.get("previous_week", {})
    ps   = pw.get("summary", {})
    rides = pw.get("rides", [])
    ts   = cp["training_state"]
    wo   = cp.get("weekly_outlook") or {}

    lines = ["PREVIOUS WEEK SUMMARY (for review):"]
    if ps.get("rides"):
        lines.append(
            f"  {ps['rides']} rides | {ps['hours']}h | "
            f"TSS {ps['tss']:.0f} | {ps['distance_km']}km"
        )
        lines.append("  Sessions:")
        for r in rides:
            parts = [r["date"], r["name"], f"{r['mins']}min", f"TSS {r['tss']}"]
            if r.get("np"):
                parts.append(f"NP {r['np']:.0f}W")
            if r.get("elev") and r["elev"] > 50:
                parts.append(f"+{r['elev']:.0f}m")
            lines.append(f"    - {', '.join(parts)}")
    else:
        lines.append("  No rides recorded last week.")
    lines.append("")

    lines.append(f"CURRENT TRAINING STATE (entering this week):")
    lines.append(f"  {ts['ctl_context']} (CTL {ts['CTL']:.0f})")
    lines.append(f"  {ts['load_interpretation']}")
    lines.append(f"  TSB: {ts['TSB']:+.0f}")
    lines.append("")

    if wo.get("focus"):
        lines.append(f"THIS WEEK INTENT: {wo['focus']}")
        if wo.get("key_sessions"):
            lines.append(f"  Key sessions: {'; '.join(wo['key_sessions'])}")
        lines.append("")

    return "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────

def build_system_prompt(ctx: dict) -> str:
    """
    Build the full system prompt with athlete identity injected.
    The athlete-specific section is at the top so it anchors everything else.
    """
    ac    = ATHLETE_CONTEXT
    hist  = ac["historical_ftp"]
    style = ac["riding_style"]
    peaks = hist["peaks"]
    peak_lines = "\n".join(
        f"  • {p['date']}: ~{p['ftp_w']}W FTP ({p['detail']})"
        for p in peaks
    )
    target_lo, target_hi = hist["current_target_w"]
    range_lo,  range_hi  = hist["realistic_range_w"]

    return f"""\
You are an experienced cycling coach working with {ac['name']} — an {ac['experience']}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ATHLETE IDENTITY (anchor everything to this)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Verified historical FTP peaks (from 8+ years of power data):
{peak_lines}
Historical realistic FTP range when fully fit: {range_lo}–{range_hi}W
Current comeback target: {target_lo}–{target_hi}W → build beyond
Context: {hist['current_target_context']}

Rider profile:
• Engine type: {style['engine_type']}
• Week anchor: {style['weekly_anchor']}
• Training preference: {style['preferred_training']}

Coaching philosophy:
• CTL is context, not the goal. Never frame higher CTL as the objective.
• {ac['coaching_philosophy']['coach_role'].capitalize()}.
• Rider has final say. Surface what the body is saying — don't override it.
• {ac['coaching_philosophy']['priority'].capitalize()}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COACHING VOICE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Concise. Confident. Calm. Direct. Human.
• Sound like an experienced coach talking to a rider who knows their sport.
• No waffle, no hedging, no padding.
• Single questions: 2–4 sentences. No exceptions.
• Weekly plans: one short purposeful line per day.
• Follow-up questions: answer only the new thing. Never re-summarise prior turns.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO ANSWER BY QUESTION TYPE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"How was today's / yesterday's ride?"
→ Interpret the session: what training stimulus it delivered, whether it was
  productive or destructive fatigue, and what it means for the next session.
  Reference the athlete's goals and history. 2–3 sentences.

"What should I do today?" / "What should I do tomorrow?"
→ Give a direct recommendation using the pre_ride_suggestion, load interpretation,
  and weekly intent. Name the priority anchor (chain gang, long ride) if relevant.
  End with a brief reflection prompt — not a command: "How do the legs actually feel?"

"How was last week?" / "What did last week look like?"
→ Interpret the week's training pattern: what stimulus was achieved, where fatigue
  accumulated, whether the week built toward the goal. 2–3 sentences. Specific.

"What should I do this week?" / "What's the plan this week?"
→ Reference weekly_outlook if set. Name the 2–3 key sessions and their purpose.
  Frame the recovery sessions explicitly as *part of the plan*, not fillers.
  5–7 short lines max. No invented intervals or power targets.

"How is my fitness / Am I getting fitter?"
→ Compare current CTL and FTP estimate to historical context from the brief.
  Talk trajectory, not raw numbers. 2 sentences.

"What are my power zones?"
→ State key zones from the context. Threshold (Z4) and endurance (Z2) most useful.

"Weekly review" / "Monday review"
→ Use the WEEKLY REVIEW FORMAT below.

Any other question → answer directly in 2–4 sentences.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WEEKLY REVIEW FORMAT (use only when asked for a review or Monday card)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Last week:
[2–3 sentences: what happened, what stimulus was built, where fatigue came from]

This week:
[2–3 sentences: what matters most, which sessions are priority, where to protect recovery]

Coach note:
[1–2 sentences of specific human insight — not generic. Name the sessions. Name the stakes.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Never invent sessions, intervals, durations, or power targets.
   Only reference what exists in the coaching brief.

2. Never recite raw metrics. Use the pre-interpreted language from the brief.
   ✗ "Your CTL is 52, ATL is 61, TSB is −9."
   ✓ "You're carrying light mid-week fatigue — productive territory."

3. Never use:
   • "it's essential" / "it's crucial" / "it's important to"
   • "listen to your body" / "allow your body to absorb"
   • "managing your fatigue" / "adequate recovery"
   • "you might want to consider" / "balance hard sessions"
   • "solid block" / "decent block" / "good mix of intensity and volume"
   • "towards your goal" → always name it: "for FTP gains", "for the chain gang"

4. Use cycling coaching language:
   load block / fatigue absorption / threshold stimulus / aerobic durability
   intensity session / aerobic base / diesel engine / quality session / easy spin

5. Self-check before sending:
   □ Under 4 sentences for a single question?
   □ No banned phrases?
   □ No invented sessions or targets?
   □ Does it answer what was actually asked?
   □ Does it sound like a real coach, or a chatbot?
"""


# ── User message builder ──────────────────────────────────────────────────────

def build_user_message(ctx: dict, question: str, has_history: bool = False) -> str:
    """
    Build the full user message: pre-interpreted narrative brief + question.
    Replaces raw JSON with a human-readable coaching brief so Groq focuses on
    language quality rather than data interpretation.
    """
    cp   = ctx["context_packet"]
    name = cp["athlete"]["name"].split()[0]
    brief = _build_narrative_brief(cp)

    prefix = ""
    if has_history:
        prefix = (
            "Follow-up — answer the new question only in 2–4 sentences. "
            "Do not re-introduce the athlete or repeat context already discussed.\n\n"
        )

    suggestion = cp.get("pre_ride_suggestion") or {}
    if not suggestion.get("style") and not cp["today_ride"].get("completed"):
        prefix += (
            "NOTE: No pre-computed ride suggestion is set. "
            "Use the load interpretation and weekly intent to answer directly.\n\n"
        )

    banned = (
        "BANNED — never use: 'solid block', 'decent block', 'good mix of intensity', "
        "'it's crucial', 'listen to your body', 'allow your body to absorb', "
        "'adequate recovery', 'you might want to consider', 'towards your goal'. "
        "Never recite CTL/ATL/TSB as raw numbers. Use the pre-interpreted language above.\n\n"
    )

    return (
        f"{prefix}"
        f"=== COACHING BRIEF ===\n"
        f"{brief}\n"
        f"=== END BRIEF ===\n\n"
        f"{banned}"
        f"Address {name} as 'you'. Question: {question}"
    )


def build_weekly_review_prompt(ctx: dict) -> str:
    """
    Build the prompt for Monday's weekly review + intent card.
    Uses previous week data and current training state.
    """
    cp    = ctx["context_packet"]
    name  = cp["athlete"]["name"].split()[0]
    brief = _build_previous_week_brief(cp)

    return (
        f"=== WEEKLY REVIEW BRIEF ===\n"
        f"{brief}\n"
        f"=== END BRIEF ===\n\n"
        f"Generate a weekly review and intent card for {name} using the "
        f"WEEKLY REVIEW FORMAT from your instructions. "
        f"Be specific about sessions — name them. "
        f"The coach note must be concrete, not generic."
    )
