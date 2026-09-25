"""
CirKit API - Pillar 1: Demand planner (LangGraph + Llama on Groq).
Run locally:  uvicorn main:app --reload   then open http://localhost:8000/docs to test.
"""
import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv()  # reads GROQ_API_KEY from backend/.env

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agents import orchestrator
from core.data import CALENDAR
from core.state import STATE

app = FastAPI(title="CirKit API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def summarise(record):
    d = record["dishes"].values()
    return {"date": record["date"], "label": record["label"], "note": record["note"],
            "footfall": record["footfall"], "cooked": sum(x["prepared"] for x in d),
            "sold": sum(x["sold"] for x in d), "leftover": sum(x["leftover"] for x in d),
            "stockouts": sum(x["stockout"] for x in d)}


@app.get("/")
def health():
    return {"status": "ok", "service": "CirKit API", "llm": "set" if os.getenv("GROQ_API_KEY") else "missing (rules fallback)"}


@app.get("/api/state")
def get_state():
    nxt = STATE.next_date
    return {"window": [summarise(r) for r in STATE.window], "next_date": nxt,
            "next": CALENDAR.get(nxt), "has_future": STATE.has_future(), "plan": STATE.plan,
            "approved": STATE.approved_order, "impact": STATE.impact, "history": STATE.history}


@app.get("/api/plan/stream")
async def plan_stream(stress: bool = False):
    async def events():
        if not STATE.has_future():
            yield f"data: {json.dumps({'type': 'error', 'msg': 'Demo data ends on 30 Sep. Press Reset to start again.'})}\n\n"
        else:
            try:
                for event in orchestrator.plan_day(STATE, stress):
                    yield f"data: {json.dumps(event)}\n\n"
                    await asyncio.sleep(0.45)  # pace the trace so people can follow the agents
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'msg': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/order/approve")
def approve():
    try:
        return {"messages": orchestrator.approve_order(STATE)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/day/close")
def close_day():
    try:
        return orchestrator.close_day(STATE)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/reset")
def reset():
    STATE.reset()
    return {"status": "reset", "next_date": STATE.next_date}
