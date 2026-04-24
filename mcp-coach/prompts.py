"""
prompts.py — Context assembly and prompt architecture for mcp-coach v2.

Architecture:
  1. tools.py fetches raw data from club.db (source of truth)
  2. analysis.py interprets the data into coaching-specific language
  3. build_context() assembles everything into a structured packet
  4. _build_narrative_brief() converts the packet into a minimum coaching brief
  5. build_system_prompt() injects athlete identity and coaching framework
  6. build_user_message() sends the brief + question to Groq

Groq's job: translate the pre-interpreted brief into natural coaching language.
Groq must NOT invent advice, compute metrics, or override athlete-specific context.
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
import analysis


# ── Context assembly ──────────────────────────────────────────────────────────

def build_context(db_path: str, user_id: int) -> dict:
    """
    Assemble the full grounded context packet.
    All numbers come from the DB — analysis.py does the interpretation.
    """
    readiness       = get_readiness_summary(db_path, user_id)
    today_rides     = get_today_rides(db_path, user_id)
    recent_rides    = get_recent_rides(db_path, user_id, days=14)
    prev_week_rides = get_previous_week_rides(db_path, user_id)
    prev_week_sum   = get_previous_week_summary(db_path, user_id)
    week            = get_week_summary(db_path, user_id)
    goal            = get_training_goal(db_path, user_id)
    zones           = get_current_zones(db_path, user_id)
    profile         = get_athlete_profile(db_path, user_id)
    ftp_est         = estimate_ftp_candidate(db_path, user_id)
    weekly_outlook  = get_weekly_outlook(db_path, user_id)

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

    # Prev CTL from metrics_cache (added in v2)
    prev_ctl = float(readiness.get("prev_ctl", 0))

    # Week targets
    tss_target   = float(goal.get("weekly_tss_target") or 0)
    hours_target = float(goal.get("weekly_hours_target") or 0)
    weekly_tss   = float(week.get("weekly_tss", 0))
    weekly_hours = float(week.get("weekly_hours", 0))
    week_tss_pct = round(weekly_tss / tss_target * 100) if tss_target else None
    week_hrs_pct = round(weekly_hours / hours_target * 100) if hours_target else None

    # Coaching intelligence (analysis layer)
    season = analysis.get_season()
    intelligence = analysis.build_coaching_intelligence(
        ctl=ctl, atl=atl, tsb=tsb, prev_ctl=prev_ctl, ftp=ftp,
        day_of_week=day_num,
        weekly_tss=weekly_tss,
        weekly_tss_target=tss_target,
        weekly_rides=int(week.get("weekly_rides", 0)),
        recent_rides=recent_rides,
        today_rides=today_rides,
        season=season,
    )

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

    # Previous week rides
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

    context_packet = {
        "date":     today_str,
        "day":      today_day,
        "is_monday": is_monday,
        "days_remaining_this_week": days_left,
        "season":   season,

        "athlete": {
            "name":          profile.get("name", "Athlete"),
            "ftp":           ftp,
            "ftp_context":   intelligence["ftp_context"],
            "goal":          goal.get("goal") or goal.get("goal_custom") or "Not set",
            "target_event":  f"{goal.get('target_event_name','')} {goal.get('target_event_date','')}".strip() or None,
            "long_ride_day": goal.get("long_ride_day") or None,
        },

        "training_state": {
            "CTL":              round(ctl, 1),
            "ATL":              round(atl, 1),
            "TSB":              round(tsb, 1),
            "prev_CTL":         round(prev_ctl, 1),
            "ratio_ATL_CTL":    ratio,
            "classification":   readiness.get("classification"),
            "readiness_score":  readiness.get("readiness"),
            "ctl_context":      intelligence["ctl_context"],
            "race_fitness":     intelligence["race_fitness"],
            "ctl_trend":        intelligence["ctl_trend"],
            "load_state":       intelligence["load_state"],
        },

        "coaching_intelligence": intelligence,

        "today_ride": today_ctx,

        "pre_ride_suggestion": {
            "style":     readiness.get("ride_style"),
            "minutes":   readiness.get("suggested_duration_minutes"),
            "tss":       readiness.get("suggested_tss"),
            "rationale": readiness.get("ride_rationale"),
        },

        "week_so_far": {
            "rides":        week.get("weekly_rides", 0),
            "hours":        weekly_hours,
            "tss":          round(weekly_tss, 0),
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
    Minimum context object — what Groq reads.
    Pre-interpreted, athlete-specific, no raw metrics dumped.
    Every line is a coaching signal, not a data readout.
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
    intel = cp.get("coaching_intelligence") or {}

    # ── Date, athlete, season ─────────────────────────────────────────────────
    lines.append(f"TODAY: {cp['day']} {cp['date']}")
    lines.append(f"ATHLETE: {ath['name']}")
    lines.append(f"FTP: {ath['ftp_context']}")
    if intel.get("season_context"):
        lines.append(f"SEASON: {intel['season_context']}")
    goal_str = ath.get("goal") or ""
    if goal_str and goal_str != "Not set":
        lines.append(f"GOAL: {goal_str}")
    if ath.get("target_event"):
        lines.append(f"TARGET EVENT: {ath['target_event']}")
    lines.append("")

    # ── Training state (interpreted, not raw) ─────────────────────────────────
    lines.append("TRAINING STATE:")
    lines.append(f"  Fitness: {ts['ctl_context']} (CTL {ts['CTL']:.0f})")
    if ts.get("ctl_trend"):
        lines.append(f"  Trend: {ts['ctl_trend']}")
    lines.append(f"  Form: {ts['load_state']}")
    if ts.get("race_fitness"):
        lines.append(f"  Race fitness: {ts['race_fitness']}")
    lines.append("")

    # ── Priority of the week ──────────────────────────────────────────────────
    if intel.get("priority_of_week"):
        lines.append(f"PRIORITY THIS WEEK:")
        lines.append(f"  {intel['priority_of_week']}")
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
        if intel.get("last_ride_assessment"):
            lines.append(f"  Assessment: {intel['last_ride_assessment']}")
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

    # ── Last ride assessment (if no today ride) ───────────────────────────────
    if not tod.get("completed") and intel.get("last_ride_assessment") and rec:
        lines.append(f"LAST RIDE: {intel['last_ride_assessment']}")
        lines.append("")

    # ── This week so far ──────────────────────────────────────────────────────
    week_line = (
        f"THIS WEEK: {wk['rides']} rides, "
        f"{wk['hours']}h, TSS {wk['tss']:.0f}"
    )
    if wk.get("tss_target"):
        pct = wk.get("tss_pct") or 0
        week_line += f" ({pct}% of {wk['tss_target']:.0f} target)"
    lines.append(week_line)
    lines.append(f"Days remaining: {cp['days_remaining_this_week']}")
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
            lines.append(f"  Note: {wo['coaching_note']}")
        lines.append("")

    # ── FTP estimate from recent rides ────────────────────────────────────────
    if cp.get("ftp_estimate_from_rides"):
        lines.append(f"FTP ESTIMATE (from recent rides): {cp['ftp_estimate_from_rides']}W")
        lines.append("")

    return "\n".join(lines)


def _build_previous_week_brief(cp: dict) -> str:
    """
    Focused brief for the previous week — used in Monday weekly review.
    """
    pw    = cp.get("previous_week", {})
    ps    = pw.get("summary", {})
    rides = pw.get("rides", [])
    ts    = cp["training_state"]
    wo    = cp.get("weekly_outlook") or {}
    intel = cp.get("coaching_intelligence") or {}

    lines = ["PREVIOUS WEEK:"]
    if ps.get("rides"):
        lines.append(
            f"  {ps['rides']} rides | {ps['hours']}h | "
            f"TSS {ps['tss']:.0f} | {ps['distance_km']}km"
        )
        for r in rides:
            parts = [r["date"], r["name"], f"{r['mins']}min", f"TSS {r['tss']}"]
            if r.get("np"):
                parts.append(f"NP {r['np']:.0f}W")
            if r.get("elev") and r["elev"] > 50:
                parts.append(f"+{r['elev']:.0f}m")
            lines.append(f"  - {', '.join(parts)}")
    else:
        lines.append("  No rides recorded last week.")
    lines.append("")

    lines.append("ENTERING THIS WEEK:")
    lines.append(f"  {ts['ctl_context']} (CTL {ts['CTL']:.0f})")
    lines.append(f"  {ts['load_state']}")
    if intel.get("race_fitness_interp"):
        lines.append(f"  {intel['race_fitness_interp']}")
    lines.append("")

    if intel.get("priority_of_week"):
        lines.append(f"THIS WEEK'S PRIORITY:")
        lines.append(f"  {intel['priority_of_week']}")
        lines.append("")

    if wo.get("focus"):
        lines.append(f"WEEK INTENT: {wo['focus']}")
        if wo.get("key_sessions"):
            lines.append(f"  Key sessions: {'; '.join(wo['key_sessions'])}")
        lines.append("")

    if intel.get("season_context"):
        lines.append(f"SEASON: {intel['season_context']}")

    return "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────

def build_system_prompt(ctx: dict) -> str:
    """
    Build the system prompt with full athlete identity, biography, and coaching framework.
    This is the coach persona — grounded in this specific athlete's history.
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

    # Significant events from timeline (health events only)
    timeline_health = [e for e in ac.get("timeline", []) if e["type"] == "health"]
    timeline_lines = "\n".join(
        f"  • {e['date']}: {e['note']}" for e in timeline_health
    )

    return f"""\
You are an experienced cycling coach working with {ac['name']}.
{ac['name']} is an experienced club rider — treat them as a peer who understands training.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ATHLETE BIOGRAPHY — anchor every response to this
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Verified FTP peaks (from 8+ years of power data):
{peak_lines}
Full-fitness range: {range_lo}–{range_hi}W | Comeback target: {target_lo}–{target_hi}W

Peak form training pattern (12 weeks before 2019 peak):
  {hist['peak_form_training_pattern']}

Rider profile:
  Engine type: {style['engine_type']}
  Weekly anchor: {style['weekly_anchor']}
  Race history: {style['race_history']}
  Comeback pattern: {style['comeback_pattern']}

Significant events that affect data interpretation:
{timeline_lines}

Coaching philosophy:
  • CTL is context, not the goal
  • Coach's job: interpret the pattern. The athlete interprets their body.
  • Quality adaptation over chasing load numbers
  • After significant health interruptions, progressive loading > hitting targets

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COACHING VOICE — read this carefully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are the rider's coach and club mate. You ride too. You speak like a real
person who understands what it's actually like to train and race.

NOT like this (AI fitness app voice — banned):
  ✗ "This aligns well with your aerobic volume priority."
  ✗ "Consider incorporating some threshold efforts."
  ✗ "This should support your chain gang work."
  ✗ "Avoid unnecessary threshold efforts."
  ✗ "It's important to manage your fatigue."

LIKE THIS (real coach voice):
  ✓ "That ride did its job — good aerobic base work."
  ✓ "Don't let your mate turn it into a race today."
  ✓ "The goal is arriving at the test with good legs — keep this one easy."
  ✓ "Chain gang Tuesday is the week. Everything else serves it."
  ✓ "Skip chain gang. Protect the FTP test."
  ✓ "You'll feel that on Tuesday if you push today."
  ✓ "Tuesday was heavy — eat, sleep, and let it absorb."
  ✓ "You're in form. Use it."

Rules of thumb:
  • Always address the athlete as "you". NEVER "we", NEVER "let's", NEVER "I'd advise".
  • Make a clear call. If the answer is 'easy ride today', say it clearly.
    Don't hedge with 'if you're feeling good you could consider...'
  • Always name what the decision protects.
    BAD: "Avoid hard efforts today."
    GOOD: "Keep this one easy — the goal is arriving at Tuesday's chain gang fresh."
  • Do NOT quote the brief's priority word-for-word. Translate it.
    BAD: "You're in a secondary quality window."
    GOOD: "There's room for one more good session today."
  • If the athlete proposes doing something different (FTP test, race, event),
    ENGAGE WITH THE ACTUAL DECISION — don't just defend the brief.
    A real coach looks at the form and says: "The numbers support it, go for it" or
    "Not the right week for a test — you're too fatigued, you'll underperform."
    For an FTP test: look at TSB. TSB > -10: "Form is decent, go for it."
    TSB < -20: "Too much fatigue carry — you'll underperform. Wait 3-4 days."
    If they've already decided, confirm it and optimise around it.
    "Skip chain gang. Keep today easy. Test with fresh legs tomorrow."
  • 3–5 sentences. No more.
  • No re-summarising context the athlete already knows.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO ANSWER BY QUESTION TYPE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"How was today's / yesterday's ride?"
→ Name the session. What did it deliver? (threshold stimulus / base miles /
  recovery / junk miles). What does it mean for the next session?
  3 sentences. Be direct: "That was a solid threshold hit." not "This session
  appears to have delivered threshold-level stimulus."

"What should I do today?" / "What should I do this session?"
→ Make a clear call first. One sentence. Then explain why and what it protects.
  CONTEXT RULE: if the athlete has mentioned an upcoming event or change of
  plan in this conversation, use that — don't default to the brief's priority.
  Do NOT ask "how do the legs feel?" or "how are you feeling today?"

"How was last week?" / "What did last week look like?"
→ Name the key sessions. What was the week's stimulus? Where did fatigue
  land? One sentence on what it means entering this week.

"What should I do this week?" / "What's the plan?"
→ Lead with the single most important thing this week. Then 2–3 sessions by
  name. Recovery days are part of the plan — say why they're there.
  5–7 short lines. No invented intervals or power targets.

"How is my fitness / Am I getting fitter?"
→ Compare current CTL and FTP to historical peaks and comeback trajectory.
  Talk direction, not numbers. 2–3 sentences.

"Am I ready for [event] / hill climb / race?"
→ Direct answer: ready / nearly / needs X more weeks. Name what's missing
  if not ready. Use the hill_climb_readiness or race_fitness_interp from brief.

"What are my power zones?"
→ Z2 (endurance) and Z4 (threshold) are the useful ones. State them.

"Weekly review" / "Monday review"
→ Use the WEEKLY REVIEW FORMAT below.

Any other question → answer directly in 3–5 sentences.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WEEKLY REVIEW FORMAT (use only for Monday card or explicit review requests)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Last week:
[2–3 sentences: what happened, what stimulus was built, where fatigue came from.
 Name the sessions. Specific TSS or duration if relevant.]

This week:
[2–3 sentences: what matters most, which sessions are priority, where to protect recovery.
 Reference chain gang Tuesday explicitly. Reference long ride if relevant.]

Coach note:
[1–2 sentences of specific human insight — not generic. Reference this athlete's
 history, comeback, or pattern where relevant. Not therapy; coaching.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Never invent sessions, intervals, durations, or power targets.
   Only reference what exists in the coaching brief.

2. Never recite raw metrics.
   ✗ "Your CTL is 61, ATL is 70, TSB is −9."
   ✓ Use the pre-interpreted load_state, ctl_context, priority_of_week.

3. BANNED phrases — never use:
   • "listen to your body" / "how do the legs feel"
   • "allow your body to absorb" / "adequate recovery"
   • "it's crucial" / "it's important to" / "it's essential"
   • "solid block" / "decent block" / "good mix of intensity"
   • "you might want to consider" / "managing your fatigue"
   • "towards your goal" → name it specifically
   • "balance hard sessions" / "don't overdo it"

4. Use cycling coaching language:
   load block / fatigue absorption / threshold stimulus / aerobic durability
   chain gang / quality session / easy spin / diesel engine / aerobic base

5. Self-check before sending:
   □ Does it answer what was actually asked?
   □ Under 5 sentences for a single question?
   □ No banned phrases?
   □ No invented sessions?
   □ Does it sound like a real coach, or a therapy bot?
   □ Did I reference the priority_of_week if session advice was asked?
"""


# ── User message builder ──────────────────────────────────────────────────────

def build_user_message(ctx: dict, question: str, has_history: bool = False) -> str:
    """
    Build the user message: minimum coaching brief + question.
    """
    cp    = ctx["context_packet"]
    name  = cp["athlete"]["name"].split()[0]
    brief = _build_narrative_brief(cp)

    prefix = ""
    if has_history:
        prefix = (
            "Follow-up — answer the new question only in 3–5 sentences. "
            "Do not re-introduce context already discussed.\n\n"
        )

    suggestion = cp.get("pre_ride_suggestion") or {}
    intel = cp.get("coaching_intelligence") or {}
    if not suggestion.get("style") and not cp["today_ride"].get("completed"):
        prefix += (
            "NOTE: No pre-computed ride suggestion. "
            "Use priority_of_week and load_state to answer directly.\n\n"
        )

    banned = (
        "BANNED: 'solid block', 'decent block', 'good mix', 'listen to your body', "
        "'how do the legs feel', 'it's crucial', 'allow your body to absorb', "
        "'adequate recovery', 'you might want to consider', 'towards your goal', "
        "'balance hard sessions'. Never recite CTL/ATL/TSB as raw numbers.\n\n"
    )

    return (
        f"{prefix}"
        f"=== COACHING BRIEF ===\n"
        f"{brief}\n"
        f"=== END BRIEF ===\n\n"
        f"{banned}"
        f"Address {name} as 'you'. Answer in 3–5 sentences maximum. "
        f"Question: {question}"
    )


def build_weekly_review_prompt(ctx: dict) -> str:
    """
    Build the Monday weekly review + intent card prompt.
    """
    cp    = ctx["context_packet"]
    name  = cp["athlete"]["name"].split()[0]
    brief = _build_previous_week_brief(cp)

    banned = (
        "BANNED — never use in the review: 'it's crucial', 'it's important to', "
        "'listen to your body', 'allow your body to absorb', 'adequate recovery', "
        "'solid block', 'decent block', 'good mix of intensity', "
        "'you might want to consider', 'manage your fatigue', 'balance hard sessions', "
        "'we need to', 'we should'. "
        "Address the athlete as 'you', never 'we'. "
        "Never recite CTL/ATL/TSB as raw numbers.\n\n"
    )

    return (
        f"=== WEEKLY REVIEW BRIEF ===\n"
        f"{brief}\n"
        f"=== END BRIEF ===\n\n"
        f"{banned}"
        f"Generate the weekly review and intent card for {name} using the "
        f"WEEKLY REVIEW FORMAT. Address {name} as 'you' throughout. "
        f"Be specific — name the sessions, reference chain gang Tuesday, "
        f"reference the priority_of_week. "
        f"The coach note must reference this athlete's comeback or history concretely."
    )
