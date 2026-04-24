"""
resources.py — Static reference data and athlete context for mcp-coach.
"""

# ── Glossary ─────────────────────────────────────────────────────────────────

GLOSSARY = {
    "CTL":  "Chronic Training Load — 42-day exponential average of daily TSS. Represents long-term fitness. Higher = fitter base.",
    "ATL":  "Acute Training Load — 7-day exponential average of daily TSS. Spikes after hard blocks, drops with rest.",
    "TSB":  "Training Stress Balance = CTL minus ATL. Positive = fresh, negative = fatigued. Trained cyclists typically train at −10 to −30.",
    "TSS":  "Training Stress Score — quantifies session load relative to FTP. 100 TSS ≈ 1 hour flat-out at FTP. A chain-gang 2.5h might be 180–220 TSS.",
    "FTP":  "Functional Threshold Power — highest average power sustainable for ~60 minutes. All zones and TSS are scaled from this number.",
    "NP":   "Normalized Power — intensity-weighted average power that accounts for variability. Better than avg watts for comparing efforts.",
    "IF":   "Intensity Factor = NP ÷ FTP. IF 0.75 = solid endurance; IF 0.90+ = threshold territory; IF 1.0+ = above threshold.",
    "ATL/CTL ratio": "Fatigue ratio. 1.0–1.5 = normal. 1.5–2.0 = active training block for a trained rider. Above 2.5 = genuinely heavy.",
    "Readiness": "0–100 score derived from TSB, adjusted for athlete fitness level (CTL). Higher = more capacity to train hard today.",
}


# ── Athlete Context ───────────────────────────────────────────────────────────
# The authoritative source of truth about who this athlete is.
# This is NOT generic — it encodes verified historical data and known
# characteristics. Groq uses this to contextualise live training data.

ATHLETE_CONTEXT = {

    # ── Identity ─────────────────────────────────────────────────────────────
    "name": "Aidan",
    "type": "road_cyclist",
    "experience": "experienced club rider — understands training, no need to over-explain",

    # ── Verified Historical FTP Peaks ────────────────────────────────────────
    # Source: Garmin historical import, validated power data (post-2018-06-25 only,
    # suspicious trainer efforts excluded). These are the trusted numbers.
    "historical_ftp": {
        "peaks": [
            {
                "date":    "2019-05-06",
                "ftp_w":   280,
                "basis":   "20min×0.95",
                "detail":  "295W/20min at HR 182bpm — verified outdoor effort",
            },
            {
                "date":    "2020-07-20",
                "ftp_w":   279,
                "basis":   "20min×0.95",
                "detail":  "294W/20min at HR 181bpm",
            },
            {
                "date":    "2023-03-24",
                "ftp_w":   275,
                "basis":   "20min×0.95",
                "detail":  "289W/20min at HR 178bpm — post-return form lifting well",
            },
        ],
        "realistic_range_w":       [270, 290],  # when at full fitness
        "current_target_w":        [240, 260],  # realistic comeback initial target
        "current_target_context":  (
            "Returning to structured training after an interrupted period. "
            "Initial target is 240–260W FTP as a sustainable comeback baseline, "
            "then progression toward and beyond previous peaks."
        ),
        "recent_best_context": (
            "Best recent efforts in spring 2023 reached 275W — shows the fitness "
            "comes back quickly when training is consistent. That's the reference point "
            "for comeback trajectory."
        ),
    },

    # ── Rider Profile ─────────────────────────────────────────────────────────
    "riding_style": {
        "engine_type": "diesel — builds and sustains aerobic work well",
        "strengths": [
            "sustained threshold and tempo efforts",
            "long aerobic volume days",
            "consistent week-over-week building",
        ],
        "not_strengths": [
            "short punchy power — not the profile",
            "random intensity spikes without aerobic base",
        ],
        "weekly_anchor": "Chain gang Tuesday — the week's priority hard session. Everything else supports it.",
        "preferred_training": (
            "Sustained aerobic volume with deliberate threshold blocks. "
            "Does not respond well to chaotic intensity. Quality over novelty."
        ),
    },

    # ── Coaching Philosophy ───────────────────────────────────────────────────
    "coaching_philosophy": {
        "rider_has_final_say":    True,
        "ctl_is_context_not_goal": True,
        "coach_role":             "interpret and reflect — not prescribe and command",
        "priority":               "quality adaptation over chasing load numbers",
        "body_awareness":         (
            "Always surface what the athlete is feeling. Offer a reflection prompt "
            "on consequential decisions. The coach interprets the data — the rider "
            "interprets their body. Both matter."
        ),
    },

    # ── Athlete Timeline Events ───────────────────────────────────────────────
    # Structured log of significant events that affect interpretation of data.
    # Add new entries here as the athlete's history expands.
    "timeline": [
        {
            "date":   "2018-06-25",
            "type":   "equipment",
            "event":  "accurate_power_meter_installed",
            "note":   (
                "Accurate power meter installed. All power data from this date "
                "is reliable and used in the historical model. Data before this "
                "date came from trial/inaccurate meters and is excluded."
            ),
        },
        # Future entries — add here when known:
        # { "date": "YYYY-MM-DD", "type": "health|equipment|event|fitness_test",
        #   "event": "slug", "note": "..." }
    ],

    # ── CTL Reference Points ─────────────────────────────────────────────────
    # Historical CTL ranges that correlate with known fitness periods.
    # Used to contextualise current CTL meaningfully.
    "ctl_reference": [
        {"ctl_range": [55, 65], "context": "strong form period — near historical FTP peaks"},
        {"ctl_range": [45, 55], "context": "solid training base — approaching strong form"},
        {"ctl_range": [35, 45], "context": "building phase — getting back toward race fitness"},
        {"ctl_range": [20, 35], "context": "early comeback or post-break rebuilding"},
    ],
}
