"""
Pillar 3 full-flow test (plus quick Pillar 1, 2 and 4 checks). Run from backend/:
    python -m pytest tests -q        or        python tests/test_pillar3.py
Uses FastAPI's TestClient, so it tests the real routes in main.py. The kitchen clock is pinned
to 13:00 IST so results do not depend on when you run it. data/state.json is backed up and restored.
"""
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core import leftover_safety as ls  # noqa: E402
from core.state import STATE, STATE_FILE, KitchenState  # noqa: E402
from main import app  # noqa: E402

_real_now = ls.now
ls.now = lambda: _real_now().replace(hour=13, minute=0)
c = TestClient(app)

FORM = {"mode": "donate", "kitchen": "Our canteen (demo)", "institution": "canteen", "dish": "Veg biryani", "qty": "35",
        "unit": "portions", "diet": "veg", "cooked_at": "12:30", "storage": "hot", "packaging": "trays",
        "recycle_kind": "", "spoiled": False, "address": "Gate 2, Meerpet", "contact": "Ravi", "phone": "98765 43210"}


def sse(path):
    with c.stream("GET", path) as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def post(form):
    r = c.post("/api/rescue/posts", json=form)
    assert r.status_code == 200, r.text
    return r.json()


def state_post(pid):
    return next(p for p in c.get("/api/rescue/state").json()["posts"] if p["id"] == pid)


def test_all():
    backup = STATE_FILE.with_suffix(".bak")
    shutil.copy(STATE_FILE, backup)
    try:
        run_flow()
    finally:
        shutil.copy(backup, STATE_FILE)
        backup.unlink()
        STATE.load()


def run_flow():
    assert 3 in c.get("/").json()["pillars"]
    routes = {r.path for r in app.routes}
    for path in ("state", "partners", "check", "posts", "stream", "accept", "decline", "request", "collected", "chat"):
        assert f"/api/rescue/{path}" in routes, path
    pr = c.get("/api/rescue/partners")
    assert pr.status_code == 200 and len(pr.json()["ngos"]) == 6 and len(pr.json()["recyclers"]) == 5
    assert [n["km"] for n in pr.json()["ngos"]] == sorted(n["km"] for n in pr.json()["ngos"])
    c.post("/api/reset")

    # 1. Share Food: exact frontend payload -> post saved and listed instantly (before any agent runs)
    r = post(FORM)
    assert not r["blocked"]
    pid = r["post"]["id"]
    assert state_post(pid)["status"] == "posted"
    fresh = KitchenState.__new__(KitchenState); fresh.load()  # survives a refresh / restart
    assert fresh.rescue_posts[0]["id"] == pid

    # clear errors, not "Not Found"
    bad = c.post("/api/rescue/posts", json={**FORM, "dish": ""})
    assert bad.status_code == 400 and "dish" in bad.json()["detail"]

    # 2. agents with stress: critic rejects the far NGO (route optimisation), then plans a route with a backup
    ev = sse(f"/api/rescue/stream?post={pid}&stress=true")
    assert any(e.get("icon") == "🔁" for e in ev), "critic should reject the stress-test match"
    p = next(e["post"] for e in ev if e["type"] == "result")
    assert p["status"] == "notified" and p["plan"]["kind"] == "donate"
    assert p["plan"]["ranked"][0] == p["partner"] and p["plan"]["backup"] and p["plan"]["margin_min"] >= 15
    assert all(k in p["fit"][p["partner"]] for k in ("in_time", "margin_min", "cap_ok", "diet_ok"))

    # 3. decline -> follow-up agent reassigns to the backup NGO
    first = p["partner"]
    d = c.post(f"/api/rescue/decline?post={pid}&ngo={first}").json()
    assert any(s["agent"] == "Follow-up" for s in d["trace"])
    assert d["post"]["partner"] != first and d["post"]["status"] == "notified"
    stages = [t["stage"] for t in d["post"]["timeline"]]
    assert "declined" in stages and "reassigned" in stages
    ngo = d["post"]["partner"]

    # 4. chat: bot answers from live data; accept; on the way; Food collected ✅ -> picked up + verified
    p = c.post("/api/rescue/chat", params={"post": pid, "sender": "ngo", "text": "Is the food still available?"}).json()["post"]
    assert p["chat"][-1]["from"] == "bot" and p["chat"][-1]["text"].startswith("🤖 Yes, 35 portions")
    assert c.post(f"/api/rescue/accept?post={pid}&ngo={ngo}").json()["post"]["status"] == "accepted"
    p = c.post("/api/rescue/chat", params={"post": pid, "sender": "ngo", "text": "On the way"}).json()["post"]
    assert p["status"] == "on_the_way"
    p = c.post("/api/rescue/chat", params={"post": pid, "sender": "ngo", "text": "Food collected ✅"}).json()["post"]
    assert p["status"] == "picked_up" and p["verified"]
    p = c.post("/api/rescue/chat", params={"post": pid, "sender": "ngo", "text": "Is the food still available?"}).json()["post"]
    assert p["chat"][-1]["text"] == "🤖 No, it has been collected."

    # 5. unsafe donate is blocked (slider flips to recycle)
    r = post({**FORM, "storage": "room", "cooked_at": "10:00"})
    assert r["blocked"] and r["switch_to"] == "recycle"

    # 6. non-veg: veg-only NGOs are never chosen; another NGO may accept directly from its card
    r = post({**FORM, "dish": "Chicken curry", "diet": "nonveg", "qty": "20"})
    ev = sse(f"/api/rescue/stream?post={r['post']['id']}")
    p = next(e["post"] for e in ev if e["type"] == "result")
    assert not ls.NGOS[p["partner"]]["veg_only"]
    veg_only = next(i for i, n in ls.NGOS.items() if n["veg_only"])
    assert c.post(f"/api/rescue/accept?post={p['id']}&ngo={veg_only}").status_code == 400
    other = next(i for i in p["plan"]["ranked"] if i != p["partner"])
    assert c.post(f"/api/rescue/accept?post={p['id']}&ngo={other}").json()["post"]["partner"] == other

    # 7. recycle: sub-type picks the partner; two pending posts join one nearest-neighbour run
    r1 = post({**FORM, "mode": "recycle", "recycle_kind": "used_oil", "dish": "Frying oil", "unit": "litres", "qty": "6"})
    sse(f"/api/rescue/stream?post={r1['post']['id']}")
    r2 = post({**FORM, "mode": "recycle", "recycle_kind": "plate_waste", "dish": "Plate waste", "unit": "kg", "qty": "8"})
    ev = sse(f"/api/rescue/stream?post={r2['post']['id']}")
    p2 = next(e["post"] for e in ev if e["type"] == "result")
    assert state_post(r1["post"]["id"])["partner"] == "uco1" and p2["partner"] == "bio1"
    run = c.get("/api/rescue/state").json()["recycle_run"]
    assert {l["id"] for l in run["legs"]} == {"uco1", "bio1"} and run["total_km"] > 0
    assert c.post(f"/api/rescue/request?post={p2['id']}&partner=gos1").status_code == 400  # goshala never takes plate waste
    assert c.post(f"/api/rescue/collected?post={p2['id']}").json()["post"]["status"] == "picked_up"

    # 8. Pillar 4 records: donation verified by the NGO's chat message, recycle by the kitchen
    s = c.get("/api/rescue/state").json()
    donation = next(x for x in s["records"] if x["mode"] == "donate")
    assert donation["verified"] and donation["verified_by"] == "NGO chat: Food collected ✅"
    assert s["totals"]["meals_donated"] == 35 and s["totals"]["verified_donations"] == 1
    assert s["totals"]["recycled_kg"]["biogas"] == 8.0
    for k in ("kg", "portions", "destination", "partner", "distance_km", "vehicle", "posted_at", "picked_up_at"):
        assert k in donation
    print("Pillar 3 + Pillar 4 records: PASS")

    # 9. Pillar 1: plan -> approve -> close still work
    c.post("/api/reset")
    s0 = c.get("/api/state").json()
    ev = sse("/api/plan/stream?stress=true")
    plan = next(e["plan"] for e in ev if e["type"] == "result")
    assert plan["date"] == s0["next_date"] and any(e.get("icon") == "🔁" for e in ev)
    assert c.post("/api/order/approve").status_code == 200
    closed = c.post("/api/day/close").json()
    assert closed["date"] == plan["date"] and c.get("/api/state").json()["impact"]["days"] == 1
    print(f"Pillar 1 plan/approve/close: PASS ({plan['footfall']} customers, forecast {closed['footfall_forecast']} vs actual {closed['footfall_actual']})")

    # 10. Pillar 2 quick check
    e = c.get("/api/expiry/state").json()
    assert any(b["source"].startswith("order") for b in e["batches"])
    ev = sse("/api/expiry/stream")
    assert any(x["type"] == "result" for x in ev)
    assert c.post("/api/expiry/clock?hours=3").status_code == 200
    print(f"Pillar 2 expiry: PASS ({len(e['batches'])} batches)")


if __name__ == "__main__":
    test_all()
    print("ALL PASS")
