"""
CirKit API - Pillar 1: Demand planner, Pillar 2: Expiry & batch tracking,
Pillar 3: Redistribution network (LangGraph + Llama on Groq).
Run locally:  uvicorn main:app --reload   then open http://localhost:8000/docs to test.
"""
import asyncio
import json
import os
from typing import List, Optional, Union

from dotenv import load_dotenv

load_dotenv()  # reads GROQ_API_KEY from backend/.env

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents import expiry_orchestrator, impact_orchestrator, orchestrator, rescue_orchestrator
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
    # "pillars" lets the website warn if it is talking to an older build of this backend
    return {"status": "ok", "service": "CirKit API", "pillars": [1, 2, 3, 4], "build": "pillar4-v1",
            "llm": "set" if os.getenv("GROQ_API_KEY") else "missing (rules fallback)"}


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


# ---------------- Pillar 2: Expiry & batch tracking ----------------

def pillar2(fn, *args):
    try:
        return fn(STATE, *args)
    except (ValueError, StopIteration) as e:
        raise HTTPException(400, str(e) or "Not found")


@app.get("/api/expiry/state")
def expiry_state():
    return expiry_orchestrator.summary(STATE)


@app.get("/api/expiry/stream")
async def expiry_stream(stress: bool = False):
    async def events():
        try:
            for event in expiry_orchestrator.plan(STATE, stress):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0.4)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/expiry/open")
def expiry_open(batch: str):
    return pillar2(expiry_orchestrator.mark_opened, batch)


@app.post("/api/expiry/clock")
def expiry_clock(hours: int = 6):
    if hours not in (1, 3, 6, 12):
        raise HTTPException(400, "Advance by 1, 3, 6 or 12 hours.")
    return pillar2(expiry_orchestrator.advance_clock, hours)


# ---------------- Pillar 3: Redistribution network ----------------
pillar3 = pillar2  # same error handling: friendly ValueError -> HTTP 400 with a readable "detail"


class FoodPost(BaseModel):
    """The kitchen's Post Food form. Fields are loose here on purpose: the orchestrator checks
    them and returns one friendly sentence (HTTP 400) instead of a raw 422 validation dump."""
    mode: str = "donate"
    kitchen: str = ""
    institution: str = ""
    dish: str = ""
    qty: Union[float, str, None] = None
    unit: str = "portions"
    diet: str = ""
    cooked_at: str = ""
    storage: str = ""
    packaging: str = ""
    recycle_kind: Optional[str] = None
    spoiled: bool = False
    address: str = ""
    contact: str = ""
    phone: str = ""


@app.get("/api/rescue/state")
def rescue_state():
    return rescue_orchestrator.summary(STATE)


@app.get("/api/rescue/partners")
def rescue_partners():
    return rescue_orchestrator.partners(STATE)


@app.post("/api/rescue/check")
def rescue_check(form: FoodPost):
    return rescue_orchestrator.precheck(STATE, form.model_dump())


@app.post("/api/rescue/posts")
def rescue_post(form: FoodPost):
    return pillar3(rescue_orchestrator.create, form.model_dump())


@app.get("/api/rescue/stream")
async def rescue_stream(post: str, stress: bool = False):
    async def events():
        try:
            for event in rescue_orchestrator.run(STATE, post, stress):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0.4)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/rescue/accept")
def rescue_accept(post: str, ngo: str):
    return pillar3(rescue_orchestrator.accept, post, ngo)


@app.post("/api/rescue/decline")
def rescue_decline(post: str, ngo: str = "", demo: bool = False):
    return pillar3(rescue_orchestrator.decline, post, ngo or None, demo)


@app.post("/api/rescue/request")
def rescue_request(post: str, partner: str):
    return pillar3(rescue_orchestrator.request_pickup, post, partner)


@app.post("/api/rescue/collected")
def rescue_collected(post: str):
    return pillar3(rescue_orchestrator.mark_collected, post)


@app.post("/api/rescue/chat")
def rescue_chat(post: str, sender: str, text: str):
    return pillar3(rescue_orchestrator.chat, post, sender, text)


# ---------------- Pillar 4: Revenue & Impact ----------------
pillar4 = pillar2


class CartItem(BaseModel):
    product: str
    vendor: str
    qty: int = 1


class Cart(BaseModel):
    items: List[CartItem]
    use_points: bool = True


class EnergyProfile(BaseModel):
    lpg_cylinders: Optional[float] = None
    electricity_units: Optional[float] = None
    fridge_age: Optional[float] = None
    solar: Optional[bool] = None
    water_kl: Optional[float] = None
    budget: Optional[float] = None


@app.get("/api/impact/state")
def impact_state():
    return impact_orchestrator.summary(STATE)


@app.get("/api/impact/stream")
async def impact_stream(stress: bool = False):
    async def events():
        try:
            for event in impact_orchestrator.plan(STATE, stress):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0.35)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/impact/approve")
def impact_approve():
    return pillar4(impact_orchestrator.approve)


@app.post("/api/impact/discard")
def impact_discard():
    return impact_orchestrator.discard(STATE)


@app.post("/api/impact/profile")
def impact_profile(profile: EnergyProfile):
    return pillar4(impact_orchestrator.set_profile, profile.model_dump(exclude_none=True))


@app.post("/api/impact/orders")
def impact_order(cart: Cart):
    return {"orders": pillar4(impact_orchestrator.place, [i.model_dump() for i in cart.items], cart.use_points)}


@app.post("/api/impact/orders/{order_id}/advance")
def impact_advance(order_id: str):
    return pillar4(impact_orchestrator.advance, order_id)


@app.get("/api/impact/ledger/verify")
def impact_verify():
    return impact_orchestrator.verify(STATE)


@app.post("/api/impact/ledger/tamper-test")
def impact_tamper():
    return impact_orchestrator.tamper(STATE)


