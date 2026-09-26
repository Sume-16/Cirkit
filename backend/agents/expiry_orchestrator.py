"""
Pillar 2 orchestrator: runs the expiry agent graph and handles the kitchen's batch actions
(mark opened, advance the demo clock).
Pillar 1 feeds it: approved purchase orders become batches, closing a day uses stock FEFO.
"""
from core import batches
from core.food_safety import ITEMS, SHELF

from . import expiry_graph

MAX_CLOCK_H = 18


def summary(state):
    view = batches.view(state)
    count = {s: sum(v["status"] == s for v in view) for s in ("expired", "critical", "at_risk", "fresh")}
    by_item = {}
    for v in view:
        if v["value_at_risk"]:
            by_item[v["item"]] = by_item.get(v["item"], 0) + v["value_at_risk"]
    return {"now": batches.iso(batches.now(state)), "clock_h": state.clock_h, "max_clock_h": MAX_CLOCK_H,
            "batches": view, "count": count, "value_at_risk": sum(v["value_at_risk"] for v in view),
            "stock_value": sum(v["value"] for v in view), "risk_by_item": by_item,
            "plan": state.expiry_plan, "impact": state.expiry_impact, "about": SHELF["_about"]}


def plan(state, stress=False):
    s = yield from expiry_graph.run(state, stress)
    state.expiry_plan = {"clock": s["clock"], "jobs": s["jobs"], "attempts": s["attempts"],
                         "source": s.get("source", "Rules"), "flagged": s.get("flagged", False)}
    state.expiry_impact["runs"] += 1
    state.save()
    yield {"type": "result", "plan": state.expiry_plan}


def mark_opened(state, batch_id):
    b = next((b for b in state.batches if b["id"] == batch_id), None)
    if b is None:
        raise ValueError(f"No batch called {batch_id}.")
    if b["opened"]:
        raise ValueError(f"{batch_id} is already opened.")
    b["opened"] = batches.iso(batches.now(state))
    state.expiry_plan = None
    state.save()
    info = ITEMS[b["item"]]
    return {"batch": batch_id, "msg": f"{batch_id} opened: shelf life is now {info['opened_hours']} h "
                                      f"(was {info['sealed_hours']} h sealed). {info['storage']}"}


def advance_clock(state, hours):
    if state.clock_h + hours > MAX_CLOCK_H:
        raise ValueError(f"The kitchen day ends after {MAX_CLOCK_H} h. Close the day in Pillar 1 to move on.")
    state.clock_h += hours
    state.expiry_plan = None
    state.save()
    return {"now": batches.iso(batches.now(state))}


# Hooks called by Pillar 1 (they only touch Pillar 2's batch ledger, never Pillar 1's numbers)
def on_order_approved(state, lines):
    for l in lines:
        if l["order"]:
            batches.receive(state, l["item"], l["order"], f"order {state.plan['date']}")
    state.expiry_plan = None


def on_day_closed(state, lines):
    for l in lines:
        batches.consume_fefo(state, l["item"], l["need"])


def on_new_day(state):
    state.clock_h = 0
    state.expiry_plan = None
