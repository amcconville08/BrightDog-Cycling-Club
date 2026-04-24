"""
groq_client.py - Groq API wrapper with graceful fallback.
If Groq is unavailable the caller receives None and should use fallback_text().
"""
import os
import re
import logging

log = logging.getLogger("mcp-coach.groq")

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            from groq import Groq
        except ImportError:
            raise RuntimeError("groq package not installed")
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY not set")
        _client = Groq(api_key=key)
    return _client


MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Post-processing substitutions: patterns the model consistently uses
# despite system prompt instructions, corrected deterministically.
_VOICE_FIXES = [
    # "we" → "you" (coach uses second person, not "we")
    (r"\bwe're\b",          "you're"),
    (r"\bwe'll\b",          "you'll"),
    (r"\bwe've\b",          "you've"),
    (r"\bwe need to\b",     "you need to"),
    (r"\bwe should\b",      "the plan is to"),
    (r"\bwe can\b",         "you can"),
    (r"\bwe want\b",        "you want"),
    # let's — must handle "let's not" before bare "let's"
    (r"\blet's not\b",      "don't"),
    (r"\blet's keep\b",     "keep"),
    (r"\blet's use\b",      "use"),
    (r"\blet's go\b",       "go"),
    (r"\blet's\b",          ""),       # fallback: drop "let's"
    # Weak advice / hedging — handle sentence-context variants first
    (r",?\s*but I'd advise against it\.?",  " — skip it."),
    (r",?\s*but I would advise against it\.?", " — skip it."),
    (r"\bI'd advise against it\.?\b",        "Skip it."),
    (r"\bI would advise against it\.?\b",    "Skip it."),
    (r"\bI'd advise\b",               ""),
    (r"\bI would advise\b",           ""),
    (r"\bI'd say\b",                  ""),
    (r"\bI would say\b",              ""),
    # Formal health-and-safety phrases
    (r"\bit's important that\b",      "make sure"),
    (r"\bit's important to\b",        "make sure you"),
    (r"\bit is important to\b",       "make sure you"),
    # Bureaucratic throat-clearing
    (r"\bIn conclusion,?\s*",         ""),
    (r"\bTo summarize,?\s*",          ""),
    (r"\bAs your coach,?\s*",         ""),
]

_BANNED_LOG = [
    "it's crucial", "it's essential",
    "listen to your body", "allow your body", "managing your fatigue",
    "adequate recovery", "balance hard sessions", "balance your training",
    "you might want to", "mix of stress and recovery",
    "solid block of training", "decent block of training",
    "good mix of intensity", "solid mix",
]


def _clean(text: str) -> str:
    """Apply voice fixes deterministically."""
    for pattern, replacement in _VOICE_FIXES:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    # Clean up artifacts from substitutions
    text = re.sub(r"\.\.+", ".", text)           # double periods → single
    text = re.sub(r",\s*\.", ".", text)           # ", ." → "."
    text = re.sub(r"\s{2,}", " ", text)           # double spaces → single
    text = re.sub(r"\.\s+([a-z])", lambda m: ". " + m.group(1).upper(), text)  # ". word" → ". Word"
    text = text.strip()
    return text


def ask(
    system_prompt: str,
    question: str,
    history: list | None = None,
    max_tokens: int = 500,
) -> str | None:
    """
    Send a grounded prompt + conversation history + user question to Groq.
    history is a list of {"role": "user"|"assistant", "content": "..."} dicts
    representing prior turns in this chat session.
    Returns the response text, or None if Groq is unavailable.
    """
    try:
        client = _get_client()
        messages = [{"role": "system", "content": system_prompt}]
        # Inject up to the last 10 turns for context, skip malformed entries
        for entry in (history or [])[-10:]:
            role    = entry.get("role", "")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content.strip()
        text = _clean(text)

        # Log banned phrase slippage for monitoring
        hits = [p for p in _BANNED_LOG if p.lower() in text.lower()]
        if hits:
            log.warning("Banned phrases in response: %s", hits)

        return text
    except Exception as exc:
        log.warning("Groq unavailable (%s: %s) — caller will use fallback", type(exc).__name__, exc)
        return None


def fallback_text(ctx: dict) -> str:
    """
    Deterministic fallback when Groq is unavailable.
    Returns plain text from the context packet — no LLM involved.
    """
    cp  = ctx.get("context_packet", {})
    ts  = cp.get("training_state", {})
    wo  = cp.get("weekly_outlook") or {}
    wk  = cp.get("week_so_far", {})
    classification = ts.get("classification", "—")
    readiness      = ts.get("readiness_score", 0)
    load_state     = ts.get("load_state", "")
    focus          = wo.get("focus", "")
    note           = wo.get("coaching_note", "")
    parts = [f"{classification} — readiness {readiness:.0f}/100. {load_state}"]
    if focus:
        parts.append(f"Week focus: {focus}. {note}")
    parts.append(
        f"This week: {wk.get('rides', 0)} rides, "
        f"{wk.get('hours', 0)}h, {wk.get('tss', 0):.0f} TSS."
    )
    return " ".join(parts)
