"""
resources.py - Static reference data for mcp-coach context assembly.
"""

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
