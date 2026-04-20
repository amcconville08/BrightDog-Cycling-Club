"""
prompts.py — Context assembly and system prompt builder for mcp-coach.

Architecture:
  Backend computes ALL metrics and coaching decisions (source of truth).
  Groq's ONLY job is to explain that data in natural language.
  Groq must NOT invent advice, compute metrics, or override the plan.
"""
from __future__ import annotations
import json
from datetime import date
from tools import (
    get_readiness_summary, get_recent_rides, get_today_rides,
    get_week_summary, get_training_goal, get_current_zones,
    estimate_ftp_candidate, get_athlete_profile, get_weekly_outlook,
)


def _ride_line(r: dict) -> str:
    mins = int(float(r["moving_time_s"]) / 60)
    km   = round(float(r["distance_m"]) / 1000, 1)
    tss  = float(r["tss"])
    np   = f", NP {r['np_watts']:.0f}W" if r.get("np_watts") else ""
    elev = f", +{r['elevation_m']:.0f}m" if r.get("elevation_m") else ""
    return f"{r['date']}: {r['name']}, {mins}min, {km}km, TSS {tss:.0f}{np}{elev}"


def build_context(db_path: str, user_id: int) -> dict:
    """Assemble the full grounded context packet. All numbers come from here — Groq sees only this."""
    readiness      = get_readiness_summary(db_path, user_id)
    today_rides    = get_today_rides(db_path, user_id)
    rides          = get_recent_rides(db_path, user_id, days=14)
    week           = get_week_summary(db_path, user_id)
    goal           = get_training_goal(db_path, user_id)
    zones          = get_current_zones(db_path, user_id)
    profile        = get_athlete_profile(db_path, user_id)
    ftp_est        = estimate_ftp_candidate(db_path, user_id)
    weekly_outlook = get_weekly_outlook(db_path, user_id)

    today_str = date.today().isoformat()
    today_day = date.today().strftime("%A")
    day_num   = date.today().weekday()   # Mon=0
    days_left = 6 - day_num

    # ── Today's completed ride(s) ──
    if today_rides:
        total_tss  = sum(float(r["tss"]) for r in today_rides)
        total_mins = sum(int(float(r["moving_time_s"]) / 60) for r in today_rides)
        today_ride_ctx = {
            "completed": True,
            "rides": [
                {
                    "name":     r["name"],
                    "duration_minutes": int(float(r["moving_time_s"]) / 60),
                    "distance_km": round(float(r["distance_m"]) / 1000, 1),
                    "tss":      round(float(r["tss"]), 1),
                    "np_watts": round(float(r["np_watts"]), 0) if r.get("np_watts") else None,
                    "elevation_m": round(float(r["elevation_m"]), 0) if r.get("elevation_m") else None,
                }
                for r in today_rides
            ],
            "total_tss":      round(total_tss, 1),
            "total_minutes":  total_mins,
        }
    else:
        today_ride_ctx = {"completed": False}

    # ── Recent rides (excl. today) ──
    recent = [
        {
            "date":  r["date"],
            "name":  r["name"],
            "mins":  int(float(r["moving_time_s"]) / 60),
            "tss":   round(float(r["tss"]), 1),
            "np":    round(float(r["np_watts"]), 0) if r.get("np_watts") else None,
        }
        for r in rides if r["date"] != today_str
    ][:6]

    # ── Training state ──
    ctl   = float(readiness.get("ctl", 0))
    atl   = float(readiness.get("atl", 0))
    tsb   = float(readiness.get("tsb", 0))
    ratio = round(atl / ctl, 2) if ctl > 0 else 0
    ftp   = float(zones.get("ftp", 200))

    # Context for load interpretation (passed to Groq so it doesn't have to decide)
    trained = ctl >= 40
    if tsb > 5:
        load_context = "Fresh — TSB positive, good time for quality or building."
    elif tsb < -35 and not trained:
        load_context = "Significant fatigue. A recovery day is justified."
    elif tsb < -35 and trained:
        load_context = (
            f"Deep in a load block (TSB {tsb:+.0f}). Normal for CTL {ctl:.0f}. "
            "Recovery should follow within 1–2 days, but hard sessions today/tomorrow are still appropriate."
        )
    elif tsb < -20:
        load_context = (
            f"Accumulating fatigue (TSB {tsb:+.0f}) — typical mid-block for CTL {ctl:.0f}. "
            "Hard sessions are productive here; avoid stacking more than 2 consecutive hard days."
        )
    else:
        load_context = "Balanced load and form."

    # Week progress
    tss_target   = float(goal.get("weekly_tss_target") or 0)
    hours_target = float(goal.get("weekly_hours_target") or 0)
    week_tss_pct = round(float(week.get("weekly_tss", 0)) / tss_target * 100) if tss_target else 0
    week_hrs_pct = round(float(week.get("weekly_hours", 0)) / hours_target * 100) if hours_target else 0

    # Build the structured context packet Groq will receive
    context_packet = {
        "date":     today_str,
        "day":      today_day,
        "days_remaining_this_week": days_left,
        "athlete": {
            "name":  profile.get("name", "Athlete"),
            "ftp":   ftp,
            "goal":  goal.get("goal") or goal.get("goal_custom") or "Not set",
            "target_event": f"{goal.get('target_event_name','')} {goal.get('target_event_date','')}".strip() or None,
            "long_ride_day": goal.get("long_ride_day") or None,
        },
        "training_state": {
            "CTL":              round(ctl, 1),
            "ATL":              round(atl, 1),
            "TSB":              round(tsb, 1),
            "ratio_ATL_CTL":    ratio,
            "classification":   readiness.get("classification"),
            "readiness_score":  readiness.get("readiness"),
            "load_context":     load_context,
        },
        "today_ride": today_ride_ctx,
        "pre_ride_suggestion": {
            "style":     readiness.get("ride_style"),
            "minutes":   readiness.get("suggested_duration_minutes"),
            "tss":       readiness.get("suggested_tss"),
            "rationale": readiness.get("ride_rationale"),
        },
        "week_so_far": {
            "rides":  week.get("weekly_rides", 0),
            "hours":  week.get("weekly_hours", 0),
            "tss":    round(float(week.get("weekly_tss", 0)), 0),
            "tss_target":   tss_target or None,
            "hours_target": hours_target or None,
            "tss_pct":   week_tss_pct or None,
            "hours_pct": week_hrs_pct or None,
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


# ─────────────────────────────────────────────────────────────────────────────
# System + user prompt builders
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an experienced club cycling coach — concise, direct, pragmatic.
Your job is to interpret training data and give clear guidance. You sound like a
coach talking to a rider after a session: brief, purposeful, no waffle.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LENGTH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Single questions: 2–4 sentences. No exceptions.
• Weekly plans: one short line per day — session type and purpose only.
• Follow-ups: answer the new question only. Never re-summarise earlier turns.
• If you are padding, stop. Cut to the point.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DO NOT RECITE METRICS OR USE GENERIC PHRASES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Never write "CTL is X, ATL is Y, TSB is Z" — that is not coaching.
Only name a metric if it is the direct answer to the question.
Never repeat the same metric twice in one response.

Also banned — vague summaries that say nothing:
  ✗ "a solid block of training"  ✗ "a decent block of training"
  ✗ "a good mix of intensity and volume"  ✗ "solid training week"
  ✗ "productive" (as a standalone filler word)

Replace with specifics: what sessions, what stimulus, what it builds toward.

  ✗ "You had a solid block with a good mix of intensity and volume."
  ✓ "Last week's long ride and group effort gave you both aerobic volume and threshold stimulus."

  ✗ "Your CTL of 49 indicates fitness is building."
  ✓ "Your recent volume is trending upward — that supports FTP progression."

  ✗ "ATL is elevated at 70, TSB is -21, indicating fatigue."
  ✓ "You're carrying real load from this week — that's expected mid-block."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANSWER BY QUESTION TYPE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"How was today's ride?" / "How was yesterday's ride?"
→ Interpret the session: what training purpose it served and how it fits the goal.
  Be specific about effort level and what it contributed. 2–3 sentences.

"What should I do today?" / "What should I do tomorrow?"
→ Use pre_ride_suggestion if it has a style/rationale.
  If not, use load_context + weekly_outlook.key_sessions to give a direct answer.
  Reference the weekly plan if it's known (e.g. "chain gang tomorrow" from context).

"How was last week?" / "How did last week go?"
→ Interpret the previous week's training pattern: volume, intensity, and how it
  fits the stated goal. Use recent_rides and week_so_far. 2–3 sentences.

"Am I getting fitter?" / "How is my fitness trending?"
→ Comment on the training trend in plain language — not metric values.
  2 sentences max.

"Am I on target?" / "How am I tracking this week?"
→ Compare week_so_far TSS/hours to targets if set.
  Say clearly whether the pattern is on track for the goal.

"What should I do this week?"
→ Reference weekly_outlook if available: name the focus and key sessions.
  Then add a brief note on how the load state shapes the week.
  Keep it to 3–5 short lines total — session type and purpose only.
  No invented intervals, durations, or power targets.

"What are my power zones?"
→ Read from power_zones in the context. State the key zones clearly.

Any other question → answer it directly in 2–4 sentences.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Never invent prescriptions. Only reference sessions that exist in the context packet.
   No interval counts, durations, power targets, rest periods, or workout structures.

2. Never use these phrases:
   • "it's essential" / "it's crucial" / "it's important to"
   • "listen to your body" / "be careful" / "ease off"
   • "allow your body to absorb" / "managing your fatigue" / "adequate recovery"
   • "you might want to" / "consider" / "it could be"
   • "balance hard sessions" / "balance your training"
   • "towards your goal" → always name it: "for the 25-mile TT", "towards FTP gains"

3. Use cycling coaching language:
   load block / fatigue absorption / intensity stimulus / aerobic durability
   threshold work / aerobic base / absorption day / hard session / easy spin

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEN THE PLAN IS KNOWN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If the athlete mentions their sessions (chain gang Tuesday, easy Wednesday, etc.),
confirm the structure makes sense and give the rationale — confidently, no hedging.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SELF-CHECK BEFORE SENDING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
□ Is it under 4 sentences for a single question?
□ Does it avoid "CTL is X, ATL is Y, TSB is Z" recitation?
□ Does it answer what was actually asked — not what you felt like saying?
□ Any banned phrases? If yes, rewrite that sentence.
□ Any invented intervals, durations, or power targets? Remove them.
"""


def build_system_prompt(ctx: dict) -> str:
    return _SYSTEM_PROMPT


def build_user_message(ctx: dict, question: str, has_history: bool = False) -> str:
    """
    Build the full user message: context packet as JSON + the question.
    has_history=True signals this is a follow-up turn — tell Groq not to re-introduce context.
    """
    cp = ctx["context_packet"]
    name = cp["athlete"]["name"].split()[0]

    if has_history:
        continuation_note = (
            "Follow-up question — answer it directly in 2–4 sentences. "
            "Do NOT re-introduce the athlete, re-summarise earlier turns, or repeat metrics already discussed.\n\n"
        )
    else:
        continuation_note = ""

    # Flag explicitly if the backend has no ride suggestion today
    suggestion = cp.get("pre_ride_suggestion", {})
    if not suggestion.get("style"):
        suggestion_note = (
            "NOTE: The backend has no ride suggestion for today (pre_ride_suggestion is null). "
            "Do NOT invent one. Read load_context and recent_rides to give a direct answer.\n\n"
        )
    else:
        suggestion_note = ""

    # Inject weekly_outlook reminder if available
    wo = cp.get("weekly_outlook") or {}
    if wo.get("focus"):
        outlook_note = (
            f"WEEKLY OUTLOOK (use when answering week/tomorrow questions): "
            f"Focus — {wo.get('focus')}. "
            f"Key sessions — {'; '.join(wo.get('key_sessions', []))}. "
            f"Coach note — {wo.get('coaching_note', '')}.\n\n"
        )
    else:
        outlook_note = ""

    banned_reminder = (
        "BANNED phrases (never use):\n"
        "• 'it's essential' / 'it's crucial' / 'it's important to be mindful'\n"
        "• 'listen to your body' / 'allow your body to absorb' / 'managing your fatigue'\n"
        "• 'adequate recovery' / 'balance hard sessions' / 'you might want to' / 'consider'\n"
        "• 'solid block of training' / 'decent block of training' / 'good block'\n"
        "• 'good mix of intensity and volume' / 'solid mix' / 'great mix'\n"
        "• 'towards your goal' (always name it: 'for FTP gains', 'for the 25-mile TT')\n"
        "BANNED patterns: never recite 'CTL is X, ATL is Y, TSB is Z'. Use plain language.\n"
        "LENGTH: 2–4 sentences for a single question. Weekly plan: short lines per day.\n\n"
    )

    return (
        f"{continuation_note}"
        f"{suggestion_note}"
        f"{outlook_note}"
        f"{banned_reminder}"
        f"Athlete context (ground truth — do not invent or override):\n"
        f"{json.dumps(cp, indent=2)}\n\n"
        f"Address {name} directly as 'you'. "
        f"Question: {question}"
    )
