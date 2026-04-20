"""
groq_client.py - Groq API wrapper with graceful fallback.
If Groq is unavailable the caller receives None and should use fallback_text().
"""
import os
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
            temperature=0.1,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content.strip()
        # Detect banned phrases — log a warning so we can track compliance
        _BANNED = [
            "it's crucial", "it's essential", "it's important to",
            "listen to your body", "allow your body", "managing your fatigue",
            "adequate recovery", "balance hard sessions", "balance your training",
            "you might want to", "mix of stress and recovery",
            "solid block of training", "decent block of training", "good block",
            "good mix of intensity", "solid mix", "great mix",
        ]
        hits = [p for p in _BANNED if p.lower() in text.lower()]
        if hits:
            log.warning("Banned phrases detected in response: %s", hits)
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
    tsb            = ts.get("TSB", 0)
    readiness      = ts.get("readiness_score", 0)
    load_ctx       = ts.get("load_context", "")
    focus          = wo.get("focus", "")
    note           = wo.get("coaching_note", "")
    parts = [f"{classification} — readiness {readiness:.0f}/100. {load_ctx}"]
    if focus:
        parts.append(f"Week focus: {focus}. {note}")
    parts.append(
        f"This week: {wk.get('rides', 0)} rides, "
        f"{wk.get('hours', 0)}h, {wk.get('tss', 0):.0f} TSS."
    )
    return " ".join(parts)
