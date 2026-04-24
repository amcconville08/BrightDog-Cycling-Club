"""
resources.py — Static reference data and athlete context for mcp-coach.
This is the authoritative source of truth about who this athlete is.
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
# NOT generic — encodes verified historical data, known characteristics,
# life events, and coaching philosophy. Groq uses this to contextualise
# live training data.

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
                "detail":  "294W/20min at HR 181bpm — post-crash rebuild peak",
            },
            {
                "date":    "2023-03-24",
                "ftp_w":   275,
                "basis":   "20min×0.95",
                "detail":  "289W/20min at HR 178bpm — second comeback, form returning well",
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
        "peak_form_training_pattern": (
            "In the 12 weeks before the 2019 peak (March–May 2019): consistent "
            "weekly TSS of 350–500, 5–6 rides/week, chain gang Tuesday as the "
            "week's quality anchor, one long aerobic ride Saturday (4–5h). "
            "No junk volume — every session had a purpose. The form came from "
            "progressive aerobic overload, not interval stacking."
        ),
    },

    # ── Rider Profile ─────────────────────────────────────────────────────────
    "riding_style": {
        "engine_type": "diesel — builds and sustains aerobic work well",
        "strengths": [
            "sustained threshold and tempo efforts",
            "long aerobic volume days",
            "consistent week-over-week building",
            "hill climb TT pacing — controlled effort, strong finish",
        ],
        "not_strengths": [
            "short punchy power — not the profile",
            "random intensity spikes without aerobic base",
            "track-style sprinting",
        ],
        "weekly_anchor": "Chain gang Tuesday — the week's priority hard session. Everything else supports it.",
        "preferred_training": (
            "Sustained aerobic volume with deliberate threshold blocks. "
            "Does not respond well to chaotic intensity. Quality over novelty."
        ),
        "race_history": (
            "Crit racer, road racer, and hill climb TT specialist. "
            "Has won local hill climb TTs. Performs best in sustained efforts "
            "where the diesel engine counts — not sprints or bunch kicks."
        ),
        "comeback_pattern": (
            "Has rebuilt from two major injury layoffs. Fitness returns quickly "
            "once structured training is consistent — CTL can rise 1–2 points/week "
            "when sessions are purposeful. The risk is overloading the comeback. "
            "The 2023 return proved: steady base build + weekly chain gang = "
            "FTP near 275W within ~10 weeks of consistent riding."
        ),
    },

    # ── Athlete Timeline Events ───────────────────────────────────────────────
    # Structured log of significant events that affect interpretation of data.
    "timeline": [
        {
            "date":   "2018-06-25",
            "type":   "equipment",
            "event":  "accurate_power_meter_installed",
            "note":   (
                "Accurate power meter installed. All power data from this date "
                "is reliable and used in the historical model. Data before this "
                "date came from inaccurate meters and is excluded."
            ),
        },
        {
            "date":   "2019-06-08",
            "type":   "health",
            "event":  "crash_pelvis_sacrum",
            "note":   (
                "Hit by a car. Broken pelvis and sacrum. Significant recovery period "
                "requiring months off the bike. Training data shows a clear CTL collapse "
                "from this date. Rebuild took through late 2019 and 2020."
            ),
        },
        {
            "date":   "2024-09-01",
            "type":   "health",
            "event":  "brain_tumour_diagnosis",
            "note":   (
                "Brain tumour diagnosis. Asymptomatic — identified incidentally. "
                "Treatment and monitoring period followed. Training was disrupted "
                "through late 2024 and early 2025. Comeback began mid-2025 "
                "and is ongoing. This is the current recovery and rebuild phase."
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
        {
            "ctl_range": [60, 75],
            "context": "peak form territory — near or at historical FTP peaks",
            "race_fitness": "race fit",
        },
        {
            "ctl_range": [50, 60],
            "context": "strong training base — approaching peak form",
            "race_fitness": "approaching race fit",
        },
        {
            "ctl_range": [40, 50],
            "context": "solid building phase — getting back toward race fitness",
            "race_fitness": "building",
        },
        {
            "ctl_range": [25, 40],
            "context": "early comeback or post-break rebuilding",
            "race_fitness": "base building",
        },
        {
            "ctl_range": [0, 25],
            "context": "very early base — weeks away from training meaningfully",
            "race_fitness": "rebuilding",
        },
    ],

    # ── Coaching Philosophy ───────────────────────────────────────────────────
    "coaching_philosophy": {
        "rider_has_final_say":    True,
        "ctl_is_context_not_goal": True,
        "coach_role":             "interpret and reflect — not prescribe and command",
        "priority":               "quality adaptation over chasing load numbers",
        "body_awareness":         (
            "Surface what the training data says — the athlete interprets "
            "their body. Both matter. Coach reads the pattern; rider reads the signals."
        ),
        "comeback_priority": (
            "After significant interruptions, progressive loading matters more "
            "than hitting targets. The pattern of consistent weeks is the signal. "
            "Not any single session."
        ),
    },

    # ── Seasonal Training Framework ───────────────────────────────────────────
    "seasonal_framework": {
        "spring_build":         "Aerobic volume priority. Threshold supports the base, not the other way around.",
        "summer_threshold":     "Quality over volume. FTP gains come from sustained effort, not junk miles.",
        "autumn_sharpening":    "Reduce volume, elevate quality. Convert fitness to performance.",
        "winter_base":          "Endurance foundation. Consistency beats intensity in these months.",
    },
}
