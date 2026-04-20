"""
mcp-coach — FastAPI context + inference service.
Reads club.db (read-only), assembles grounded context, calls Groq.
Port 9207.
"""
import os
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import tools
import resources
import prompts
import groq_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("mcp-coach")

DB_PATH = os.environ.get("DB_PATH", "/data/club.db")

app = FastAPI(title="mcp-coach", version="1.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# /ask — main endpoint called by the cycling-club app
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    user_id: int
    question: str
    history: list = []   # prior turns: [{"role": "user"|"assistant", "content": "..."}]


@app.post("/ask")
def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    try:
        ctx    = prompts.build_context(DB_PATH, req.user_id)
        system = prompts.build_system_prompt(ctx)
        # Question is enriched with the full context packet so Groq always
        # has ground-truth data regardless of what the user asked
        has_history = bool(req.history)
        full_question = prompts.build_user_message(ctx, req.question, has_history=has_history)
        answer = groq_client.ask(system, full_question, history=req.history)
        if answer:
            source = "groq"
        else:
            answer = groq_client.fallback_text(ctx)
            source = "fallback"
        return {"answer": answer, "source": source}
    except Exception as exc:
        log.error("ask failed for user %s: %s", req.user_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# /context — full structured context (useful for debugging)
# ---------------------------------------------------------------------------

@app.get("/context/{user_id}")
def get_context(user_id: int):
    try:
        return prompts.build_context(DB_PATH, user_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# /tools/* — individual tool endpoints
# ---------------------------------------------------------------------------

@app.get("/tools/readiness/{user_id}")
def tool_readiness(user_id: int):
    return tools.get_readiness_summary(DB_PATH, user_id)

@app.get("/tools/last-ride/{user_id}")
def tool_last_ride(user_id: int):
    return tools.get_last_ride(DB_PATH, user_id)

@app.get("/tools/recent-rides/{user_id}")
def tool_recent_rides(user_id: int, days: int = 14):
    return tools.get_recent_rides(DB_PATH, user_id, days)

@app.get("/tools/week/{user_id}")
def tool_week(user_id: int):
    return tools.get_week_summary(DB_PATH, user_id)

@app.get("/tools/goal/{user_id}")
def tool_goal(user_id: int):
    return tools.get_training_goal(DB_PATH, user_id)

@app.get("/tools/zones/{user_id}")
def tool_zones(user_id: int):
    return tools.get_current_zones(DB_PATH, user_id)

@app.get("/tools/ftp-estimate/{user_id}")
def tool_ftp_estimate(user_id: int):
    return {"ftp_estimate": tools.estimate_ftp_candidate(DB_PATH, user_id)}

@app.get("/tools/coaching-brief/{user_id}")
def tool_coaching_brief(user_id: int):
    return tools.get_today_coaching_brief(DB_PATH, user_id)


# ---------------------------------------------------------------------------
# /resources/* — static reference data
# ---------------------------------------------------------------------------

@app.get("/resources/glossary")
def resource_glossary():
    return resources.GLOSSARY


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def health():
    # Verify DB is reachable
    try:
        import sqlite3
        c = sqlite3.connect(DB_PATH)
        c.execute("SELECT count(*) FROM users")
        c.close()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "db": db_ok, "model": groq_client.MODEL}
