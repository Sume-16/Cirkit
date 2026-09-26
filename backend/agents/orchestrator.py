"""
Orchestrator: runs the Pillar 1 agents in order and passes results between them.
Planning now runs as a LangGraph agent graph (see agents/graph.py).
Also closes the day: simulates what really happened, updates stock, rolls the queue.
"""
from core.data import CALENDAR, DISHES, INGREDIENTS
from core.simulator import simulate_day, usual_habits

from . import expiry_orchestrator, graph


def plan_day(state, stress=False):
    s = yield from graph.run(state, stress)
    state.plan = {"date": s["day"], "label": s["label"], "note": s["note"], "footfall": round(s["footfall"]),
                  "attempts": s["attempts"], "context_source": s["source"], "context_reason": s["reason"],
                  "bias": round(s["bias"], 2), "flagged": s.get("flagged", False), "dishes": s["dishes"],
                  "ingredients": s["lines"], "order_total": s["total"]}
    state.approved_order = False
    state.save()
    yield {"type": "result", "plan": state.plan}


def approve_order(state):
    if not state.plan:
        raise ValueError("Run the planning agents first.")
    if not state.approved_order:
        for l in state.plan["ingredients"]:
            state.stock[l["item"]] = state.stock.get(l["item"], 0) + l["order"]
        expiry_orchestrator.on_order_approved(state, state.plan["ingredients"])  # Pillar 2: tag new batches
        state.approved_order = True
        state.save()
    by_vendor = {}
    for l in state.plan["ingredients"]:
        if l["order"]:
            by_vendor.setdefault(l["vendor"], []).append(f"{l['item']} {l['order']} {l['unit']}")
    return [{"vendor": v, "message": f"Namaste! CirKit canteen order for {state.plan['date']}: "
                                     + ", ".join(items) + ". Please deliver by 7 AM."}
            for v, items in by_vendor.items()]


def portion_cost(dish):
    return sum(q * INGREDIENTS[i]["price"] for i, q in DISHES[dish]["bom"].items())


def close_day(state):
    plan = state.plan
    if not plan or plan["date"] != state.next_date:
        raise ValueError("Plan the day before closing it.")
    day = plan["date"]
    actual = simulate_day(day, {d: v["cook"] for d, v in plan["dishes"].items()})
    usual = usual_habits(day)

    for l in plan["ingredients"]:  # stock used by what was cooked
        state.stock[l["item"]] = round(max(0, state.stock[l["item"]] - l["need"]), 2)
    expiry_orchestrator.on_day_closed(state, plan["ingredients"])  # Pillar 2: batches used FEFO

    ours_left = sum(v["leftover"] for v in actual["dishes"].values())
    usual_left = sum(v["leftover"] for v in usual["dishes"].values())
    ours_out = [d for d, v in actual["dishes"].items() if v["stockout"]]
    usual_out = [d for d, v in usual["dishes"].items() if v["stockout"]]
    saved_money = sum((usual["dishes"][d]["leftover"] - actual["dishes"][d]["leftover"]) * portion_cost(d)
                      for d in DISHES)
    err = (actual["footfall"] - plan["footfall"]) / actual["footfall"] * 100

    state.history.append({"date": day, "forecast": plan["footfall"], "actual": actual["footfall"]})
    state.impact["days"] += 1
    state.impact["portions_saved"] += usual_left - ours_left
    state.impact["stockouts_avoided"] += len(usual_out) - len(ours_out)
    state.impact["money_saved"] += round(saved_money)
    dropped = state.roll(actual)
    expiry_orchestrator.on_new_day(state)  # Pillar 2: clock moves to the next morning
    state.plan, state.approved_order = None, False
    state.save()
    return {"date": day, "footfall_forecast": plan["footfall"], "footfall_actual": actual["footfall"],
            "error_pct": round(err, 1), "dishes": actual["dishes"],
            "ours": {"leftover": ours_left, "stockouts": ours_out},
            "usual": {"leftover": usual_left, "stockouts": usual_out},
            "money_saved": round(saved_money), "surplus_for_redistribution": ours_left,
            "queue": {"dropped": dropped, "added": day,
                      "window": [state.window[0]["date"], state.window[-1]["date"]]}}
