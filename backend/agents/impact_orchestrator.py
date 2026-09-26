"""
Pillar 4 orchestrator (Revenue & Impact): runs the impact graph, handles the manager's approval,
the GreenLoop Marketplace cart and demo orders with invoices, order status, the energy profile,
the SHA-256 ledger (verify + tamper test), and the revenue, government and certificate views.
Demo orders only: no real payment is processed.
"""
from core import impact as im
from core import ledger as lg
from core import leftover_safety as ls

from . import impact_graph
from .common import collect, step

STATUSES = ["placed", "vendor_confirmed", "dispatched", "delivered"]
PROFILE_LIMITS = {"lpg_cylinders": (0, 200), "electricity_units": (0, 100000), "fridge_age": (0, 40),
                  "water_kl": (0, 5000), "budget": (0, 10000000)}


def accepted(state):
    return im.verify_records(state.rescue_records)[0]


def current_scores(state):
    return im.scores(state.green["ledger"], accepted(state), state.green["profile"], state.window)


# ---------------- agent graph ----------------
def plan(state, stress=False):
    run_id, s = yield from impact_graph.run(state, stress)
    state.green["run"] = {"id": run_id, "at": ls.iso(ls.now()), "stress": stress, "status": "awaiting_approval",
                           "accepted": len(s["accepted"]), "rejected": [{"post": r["post"], "reason": r["reason"]} for r in s["rejected"]],
                           "ledger_ok": s["ledger_ok"], "scores": s["scores"], "recs": s["recs"], "lines": s["lines"],
                           "money": s["money"], "attempts": s["attempts"], "flagged": s.get("flagged", False), "orders": []}
    state.save()
    yield {"type": "result", "run": state.green["run"]}


def approve(state):
    run = state.green["run"]
    if not run or run["status"] != "awaiting_approval":
        raise ValueError("Run the impact agents first; there is no order waiting for approval.")
    orders = place(state, [{"product": l["product"], "vendor": l["vendor"], "qty": l["qty"]} for l in run["lines"]],
                   True, "agents") if run["lines"] else []
    trace = [step("Manager approval", f"Approved. {len(orders)} demo order(s) placed: "
                  + ", ".join(o["invoice_no"] for o in orders) + "." if orders else "Approved. No order to place.", "✅")]
    try:
        t, report = collect(impact_graph.resume(run["id"]))
        trace += t
    except KeyError:  # server restarted since the run: the paused graph is gone, so report directly
        report = impact_graph.write_report(run["scores"], run["lines"])
        trace.append(step("Reporter", f"{report['by']} wrote the ESG summary and the 3-line city summary.", "📰"))
    run.update({"status": "approved", "orders": [o["id"] for o in orders]})
    state.green["report"] = {**report, "at": ls.iso(ls.now())}
    state.save()
    return {"trace": trace, "orders": orders, "report": state.green["report"]}


def discard(state):
    if state.green["run"]:
        state.green["run"]["status"] = "discarded"
        state.save()
    return {"ok": True}


# ---------------- marketplace orders ----------------
def place(state, items, use_points=True, source="cart"):
    """Cart items -> one demo order + invoice per vendor. 1 point = Rs 1 off, max 10% per order."""
    if not items:
        raise ValueError("The cart is empty.")
    imp, lines = state.green, []
    for it in items:
        key, vendor, qty = it.get("product"), it.get("vendor"), int(it.get("qty") or 1)
        o = next((x for x in im.offers(key) if x["vendor"] == vendor), None) if key in im.PRODUCTS else None
        if not o or not 1 <= qty <= 50:
            raise ValueError(f"{key} from {vendor} is not in the catalogue (or quantity is not 1-50).")
        lines.append({**o, "name": im.PRODUCTS[key]["name"], "category": im.PRODUCTS[key]["category"], "qty": qty,
                      "amount": o["price"] * qty})
    orders = []
    for vendor in dict.fromkeys(l["vendor"] for l in lines):
        vl = [l for l in lines if l["vendor"] == vendor]
        balance = sum(e["points"] for e in imp["ledger"]) if use_points else 0
        money = im.price_lines(vl, balance)
        imp["invoice_seq"] += 1
        now = ls.iso(ls.now())
        order = {"id": f"O{imp['invoice_seq']}", "invoice_no": f"CK-2026-{imp['invoice_seq']:04d}", "date": now[:10], "created_at": now,
                 "buyer": im.CFG["buyer"], "vendor": vendor, "vendor_name": im.VENDORS[vendor]["name"], "lines": vl, **money,
                 "gst_pct": im.R["gst_pct"], "commission": round(money["taxable"] * im.R["commission_pct"] / 100),
                 "status": "placed", "history": [{"status": "placed", "at": now}], "source": source,
                 "note": "Demo invoice - no real payment is processed."}
        if money["discount"]:
            lg.add(imp["ledger"], now, "redeem", f"{money['discount']} Green Points redeemed on {order['invoice_no']}",
                   -money["discount"], 0, order["invoice_no"])
        imp["orders"].insert(0, order)
        orders.append(order)
    state.save()
    return orders


def advance(state, order_id):
    """Simulate the vendor: placed -> vendor confirmed -> dispatched -> delivered.
    Delivery earns the product's Green Points bonus, writes a ledger entry and updates the energy profile."""
    imp = state.green
    o = next((x for x in imp["orders"] if x["id"] == order_id), None)
    if not o:
        raise ValueError(f"No order {order_id}.")
    if o["status"] == "delivered":
        raise ValueError(f"{o['invoice_no']} is already delivered.")
    o["status"] = STATUSES[STATUSES.index(o["status"]) + 1]
    now = ls.iso(ls.now())
    o["history"].append({"status": o["status"], "at": now})
    if o["status"] == "delivered":
        p = imp["profile"]
        for l in o["lines"]:
            for k, v in im.PRODUCTS[l["product"]]["effect"].items():
                if isinstance(v, bool) or k == "fridge_age":
                    p[k] = v
                else:
                    p[k] = max(0, round(p.get(k, 0) + v * l["qty"], 1))
        pts = sum(l["points"] * l["qty"] for l in o["lines"])
        co2 = sum(l["co2"] * l["qty"] for l in o["lines"])
        lg.add(imp["ledger"], now, "upgrade", f"Delivered {', '.join(l['name'] for l in o['lines'])} ({o['invoice_no']}); "
               f"cuts about {co2} kg CO2/month (mock)", pts, co2, o["invoice_no"])
    state.save()
    return {"order": o}


def set_profile(state, data):
    p = state.green["profile"]
    for k, (lo, hi) in PROFILE_LIMITS.items():
        if k in data and data[k] is not None:
            try:
                v = float(data[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k.replace('_', ' ')} must be a number.")
            if not lo <= v <= hi:
                raise ValueError(f"{k.replace('_', ' ')} must be between {lo} and {hi}.")
            p[k] = int(v) if k in ("electricity_units", "fridge_age", "budget") else v
    if "solar" in data:
        p["solar"] = bool(data["solar"])
    state.save()
    return {"profile": p}


# ---------------- ledger ----------------
def verify(state):
    ok, seq, msg = lg.verify(state.green["ledger"])
    return {"ok": ok, "broken_at": seq, "msg": msg, "head": state.green["ledger"][-1]["hash"] if state.green["ledger"] else lg.GENESIS}


def tamper(state):
    return lg.tamper_test(state.green["ledger"])


# ---------------- views ----------------
def government(state, sc):
    kitchens = [*im.CFG["city"]["kitchens"], {"name": im.CFG["buyer"], "points": sc["earned"], "diverted_pct": sc["diverted_pct"],
                                              "meals": sc["meals"], "you": True}]
    for k in kitchens:
        k["tier"] = im.tier(k["points"])["name"]
    acc = accepted(state)
    ngos = [{"name": n["name"], "kind": n["kind"], "capacity": n["capacity"], "veg_only": n["veg_only"], "certified": True,
             "meals": sum(r["portions"] for r in acc if r["mode"] == "donate" and r["partner"] == n["name"])} for n in ls.NGOS.values()]
    by_dest = {}
    for r in acc:
        by_dest[r["destination"]] = round(by_dest.get(r["destination"], 0) + r["kg"], 1)
    city_pct = round((im.CFG["city"]["baseline_diverted_pct"] * 6 + sc["diverted_pct"]) / 7, 1)
    rep = state.green["report"] or impact_graph.report_template(sc, [])
    return {"city": im.CFG["city"]["name"], "leaderboard": sorted(kitchens, key=lambda k: -k["points"]), "ngos": ngos,
            "landfill": {"goal_pct": im.R["landfill_goal_pct"], "ours_pct": sc["diverted_pct"], "city_pct": city_pct},
            "kg_by_destination": by_dest, "city_summary": rep["city_summary"], "roadmap": im.CFG["roadmap"]}


def summary(state):
    imp = state.green
    sc = current_scores(state)
    ok, _, msg = lg.verify(imp["ledger"])
    return {"about": im.CFG["_about"], "buyer": im.CFG["buyer"], "scores": sc, "profile": imp["profile"],
            "ledger": imp["ledger"][-15:][::-1], "ledger_size": len(imp["ledger"]), "ledger_ok": ok, "ledger_msg": msg,
            "catalogue": im.catalogue(), "categories": im.CATS, "orders": imp["orders"], "statuses": STATUSES,
            "run": imp["run"], "report": imp["report"], "revenue": im.revenue(imp["orders"], accepted(state)),
            "gov": government(state, sc), "monthly_saving_rs": sum(l["saving"] * l["qty"] for o in imp["orders"]
                                                                  if o["status"] == "delivered" for l in o["lines"]),
            "rules": {k: im.R[k] for k in ("max_points_discount_pct", "gst_pct", "commission_pct", "max_payback_months",
                                           "donation_target_pct", "landfill_goal_pct")}}
