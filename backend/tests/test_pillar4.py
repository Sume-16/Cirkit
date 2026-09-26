"""
Pillar 4 (Revenue & Impact) full-graph test, including both stress tests. Run from backend/:
    python tests/test_pillar4.py        (tests/test_pillar3.py covers Pillars 1, 2 and 3)
Uses FastAPI's TestClient against the real routes. Clock pinned to 13:00 IST; data/state.json is restored.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core import leftover_safety as ls  # noqa: E402
from core.state import STATE, STATE_FILE  # noqa: E402
from main import app  # noqa: E402

_real_now = ls.now
ls.now = lambda: _real_now().replace(hour=13, minute=0)
c = TestClient(app)

FORM = {"mode": "donate", "kitchen": "Our canteen (demo)", "institution": "canteen", "dish": "Veg biryani", "qty": "30",
        "unit": "portions", "diet": "veg", "cooked_at": "12:30", "storage": "hot", "packaging": "trays",
        "recycle_kind": "", "spoiled": False, "address": "Gate 2, Meerpet", "contact": "Ravi", "phone": "9876543210"}


def sse(path):
    with c.stream("GET", path) as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def pillar3_pickups():
    """One verified donation (NGO said "Food collected ✅") and one collected recycle, via the real Pillar 3 routes."""
    pid = ok(c.post("/api/rescue/posts", json=FORM))["post"]["id"]
    p = next(e["post"] for e in sse(f"/api/rescue/stream?post={pid}") if e["type"] == "result")
    ok(c.post(f"/api/rescue/accept?post={pid}&ngo={p['partner']}"))
    ok(c.post("/api/rescue/chat", params={"post": pid, "sender": "ngo", "text": "Food collected ✅"}))
    rid = ok(c.post("/api/rescue/posts", json={**FORM, "mode": "recycle", "recycle_kind": "plate_waste", "dish": "Plate waste",
                                               "unit": "kg", "qty": "8"}))["post"]["id"]
    sse(f"/api/rescue/stream?post={rid}")
    ok(c.post(f"/api/rescue/collected?post={rid}"))


def run_flow():
    assert 4 in ok(c.get("/"))["pillars"]
    ok(c.post("/api/reset"))
    s0 = ok(c.get("/api/impact/state"))
    assert s0["scores"]["points"] == 380 and s0["ledger_ok"], "opening balance (mock) and an intact chain"
    assert len({o["vendor"] for o in s0["catalogue"]}) == 17 and all(
        len({o["vendor"] for o in s0["catalogue"] if o["category"] == cat}) >= 2 for cat in s0["categories"])
    pillar3_pickups()

    # ---- full graph with BOTH stress tests: fake donation + over-budget first recommendation
    ev = sse("/api/impact/stream?stress=true")
    steps = [e for e in ev if e["type"] == "step"]
    run = next(e["run"] for e in ev if e["type"] == "result")
    assert any("FAKE-1" in s["msg"] and s["agent"] == "Verifier" for s in steps), "fake donation must be rejected"
    assert any(r["post"] == "FAKE-1" for r in run["rejected"])
    assert any(s["agent"] == "Critic" and "over the Rs" in s["msg"] and s["icon"] == "🔁" for s in steps), "critic must catch over-budget"
    assert run["attempts"] >= 2 and run["status"] == "awaiting_approval"
    assert not any(s["agent"] == "Reporter" for s in steps), "graph must pause before the reporter"
    sc = run["scores"]
    # opening 380 + 30 meals + 8 kg x 2 + 5 per EV pickup; FAKE-1's 480 meals never count
    assert sc["meals"] == 30 and sc["points"] == 380 + 30 + 16 + 5 * sc["ev_pickups"], sc
    ledger_refs = {e["ref"] for e in ok(c.get("/api/impact/state"))["ledger"]}
    assert "FAKE-1" not in ledger_refs
    budget = ok(c.get("/api/impact/state"))["profile"]["budget"]
    assert run["lines"] and run["money"]["total"] <= budget
    assert run["money"]["discount"] <= run["money"]["subtotal"] * 0.10
    assert len({l["category"] for l in run["lines"]}) == len(run["lines"]) and all(l["payback"] <= 36 for l in run["lines"])
    print(f"Graph + stress tests: PASS (fake donation rejected, points {sc['points']}; over-budget caught, {run['attempts']} attempts; "
          f"{len(run['lines'])} upgrades, total Rs {run['money']['total']:,})")

    # ---- manager approval: orders + invoices, then the paused graph resumes into the reporter
    a = ok(c.post("/api/impact/approve"))
    assert any(t["agent"] == "Reporter" for t in a["trace"]) and a["report"]["esg_summary"] and len(a["report"]["city_summary"]) == 3
    inv = a["orders"][0]
    assert inv["invoice_no"] == "CK-2026-0001" and inv["buyer"] == "Our canteen (demo)" and "no real payment" in inv["note"]
    assert inv["total"] == inv["taxable"] + round(inv["taxable"] * 0.18) and inv["taxable"] == inv["subtotal"] - inv["discount"]
    assert inv["commission"] == round(inv["taxable"] * 0.08)  # paid by the vendor, not on the buyer's total
    st = ok(c.get("/api/impact/state"))
    assert any(e["kind"] == "redeem" and e["points"] < 0 for e in st["ledger"]) and st["ledger_ok"]
    assert c.post("/api/impact/approve").status_code == 400, "cannot approve twice"
    print(f"Approval + invoice: PASS ({', '.join(o['invoice_no'] for o in a['orders'])}; report by {a['report']['by']})")

    # ---- order status: placed -> vendor confirmed -> dispatched -> delivered (points, ledger, profile)
    before = st["profile"]
    pts_before = st["scores"]["points"]
    for _ in range(3):
        o = ok(c.post(f"/api/impact/orders/{inv['id']}/advance"))["order"]
    assert o["status"] == "delivered" and [h["status"] for h in o["history"]] == ["placed", "vendor_confirmed", "dispatched", "delivered"]
    st = ok(c.get("/api/impact/state"))
    assert st["scores"]["points"] == pts_before + sum(l["points"] for l in inv["lines"])
    assert st["ledger"][0]["kind"] == "upgrade" and st["profile"] != before and st["monthly_saving_rs"] > 0
    assert c.post(f"/api/impact/orders/{inv['id']}/advance").status_code == 400
    print(f"Order delivered: PASS (profile now {st['profile']})")

    # ---- cart: two vendors -> two invoices; points discount max 10%
    cart = {"items": [{"product": "led_kit", "vendor": "ecowatt", "qty": 2}, {"product": "aerators", "vendor": "bluedrop", "qty": 1}]}
    orders = ok(c.post("/api/impact/orders", json=cart))["orders"]
    assert len(orders) == 2 and all(o["discount"] <= o["subtotal"] * 0.10 for o in orders)
    assert c.post("/api/impact/orders", json={"items": [{"product": "led_kit", "vendor": "nobody"}]}).status_code == 400

    # ---- profile, ledger, revenue, government
    assert c.post("/api/impact/profile", json={"lpg_cylinders": -5}).status_code == 400
    assert ok(c.post("/api/impact/profile", json={"budget": 200000, "solar": True}))["profile"]["solar"] is True
    tt = ok(c.post("/api/impact/ledger/tamper-test"))
    assert tt["detected"] and tt["real_ledger_ok"] and ok(c.get("/api/impact/ledger/verify"))["ok"]
    st = ok(c.get("/api/impact/state"))
    rv = st["revenue"]
    assert len(rv["streams"]) == 5 and rv["total_rs"] == sum(s["rs"] for s in rv["streams"]) and rv["ngos_free"]
    gov = st["gov"]
    assert any(k.get("you") for k in gov["leaderboard"]) and gov["landfill"]["goal_pct"] == 30 and len(gov["ngos"]) == 6
    assert len(gov["roadmap"]) == 5 and len(gov["city_summary"]) == 3

    # ---- a second run does not award the same pickups twice
    sse("/api/impact/stream")
    assert ok(c.get("/api/impact/state"))["ledger_size"] == st["ledger_size"], "no duplicate awards"
    print(f"Cart, ledger, revenue, government: PASS (revenue Rs {rv['total_rs']:,}/month, self-sufficiency {rv['self_sufficiency_pct']}%)")


def test_pillar4():
    backup = STATE_FILE.with_suffix(".bak4")
    shutil.copy(STATE_FILE, backup)
    try:
        run_flow()
    finally:
        shutil.copy(backup, STATE_FILE)
        backup.unlink()
        STATE.load()


if __name__ == "__main__":
    test_pillar4()
    print("ALL PASS")
