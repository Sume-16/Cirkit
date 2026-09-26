"""
Pillar 4 (Revenue & Impact) rules. Plain rules, no LLM: which records count, Green Credit Points,
tiers, CO2 estimates, the upgrade advisor's rule fallback, vendor value, the critic, invoice maths
and the revenue model. All numbers come from data/marketplace.json and are MOCK ESTIMATES.
Green Credit Points are CirKit loyalty points, not carbon credits.
"""
import statistics

from . import ledger as lg
from .data import load_json
from .leftover_safety import iso, now

CFG = load_json("marketplace.json")
R, CATS, VENDORS = CFG["rules"], CFG["categories"], CFG["vendors"]
PRODUCTS = {p["key"]: p for p in CFG["products"]}
NGO_CHAT = "NGO chat: Food collected"


def seed():
    """Fresh Pillar 4 state (on reset). The opening balance is clearly labelled mock."""
    imp = {"ledger": [], "profile": {**CFG["profile"], "ev_bonus_pct": 0}, "orders": [], "invoice_seq": 0, "run": None, "report": None}
    for e in CFG["opening_ledger"]:
        lg.add(imp["ledger"], iso(now()), e["kind"], e["text"], e["points"], e["co2_kg"])
    return imp


# ---------------- catalogue ----------------
def payback(price, saving):
    return round(price / saving, 1) if saving else 999.0


def offers(key):
    p = PRODUCTS[key]
    return [{"product": key, "vendor": v, "vendor_name": VENDORS[v]["name"], "price": price, "days": days, "rating": rating,
             "saving": saving, "co2": co2, "points": pts, "payback": payback(price, saving)}
            for v, price, days, rating, saving, co2, pts in p["offers"]]


def catalogue():
    out = []
    for p in CFG["products"]:
        for o in offers(p["key"]):
            out.append({**o, "name": p["name"], "category": p["category"], "replaces": p["replaces"]})
    return out


def value(o):
    """Procurement's best-value score: saving per rupee, CO2 per rupee, rating, points bonus, fast delivery."""
    return round(o["saving"] / o["price"] * 1000 + o["co2"] / o["price"] * 1000 + o["rating"] * 2
                 + o["points"] / o["price"] * 1000 - o["days"] * 0.05, 3)


def best_offer(key):
    return max(offers(key), key=value)


def cheapest(key):
    return min(offers(key), key=lambda o: o["price"])


# ---------------- verification and scoring ----------------
def tier(earned):
    tiers = R["tiers"]
    cur = [t for t in tiers if earned >= t["min"]][-1]
    nxt = next((t for t in tiers if t["min"] > earned), None)
    prog = 100 if not nxt else round((earned - cur["min"]) / (nxt["min"] - cur["min"]) * 100)
    return {"name": cur["name"], "next": nxt["name"] if nxt else None, "to_next": (nxt["min"] - earned) if nxt else 0, "progress": prog}


def verify_records(records):
    """Only donations the NGO confirmed with "Food collected ✅" count; recycles need the kitchen's pickup.
    Spikes (bigger than any NGO can feed, or far above normal) are flagged and left out."""
    accepted, rejected = [], []
    donated = [r["portions"] for r in records if r.get("mode") == "donate" and r.get("verified")]
    typical = statistics.median(donated) if len(donated) >= 3 else None
    for r in records:
        why = None
        if not r.get("verified"):
            why = "not verified"
        elif r.get("mode") == "donate" and not str(r.get("verified_by", "")).startswith(NGO_CHAT):
            why = f"donation not confirmed by the NGO's \"Food collected ✅\" (was: {r.get('verified_by') or 'nobody'})"
        elif r.get("mode") == "donate" and r.get("portions", 0) > R["spike_max_portions"]:
            why = f"spike: {r['portions']} portions is more than any partner NGO can feed ({R['spike_max_portions']})"
        elif r.get("mode") == "donate" and typical and r["portions"] > R["spike_factor"] * typical:
            why = f"spike: {r['portions']} portions is over {R['spike_factor']}x the usual {typical:.0f}"
        (rejected if why else accepted).append({**r, "reason": why} if why else r)
    return accepted, rejected


def award(r):
    """Points and CO2 (mock estimates) for one accepted Pillar 3 record."""
    ev = "EV" in str(r.get("vehicle", ""))
    ev_km = r.get("distance_km", 0) * 2 if ev else 0
    if r["mode"] == "donate":
        pts, co2 = r["portions"] * R["points_per_meal"], r["kg"] * R["co2_per_donated_kg"]
        text = f"{r['portions']} meals of {r['dish']} to {r['partner']} (NGO confirmed)"
    else:
        pts, co2 = round(r["kg"] * R["points_per_recycled_kg"]), r["kg"] * R["co2_per_recycled_kg"]
        text = f"{r['kg']} kg {r['dish']} to {r['partner']} ({r['destination'].replace('_', ' ')})"
    if ev:
        pts += R["points_per_ev_pickup"]
        co2 += ev_km * R["co2_per_ev_km"]
        text += " · EV pickup"
    return {"kind": r["mode"], "text": text, "points": pts, "co2_kg": co2, "ref": r["post"], "ev_km": ev_km}


def scores(ledger, records, profile, window):
    """Scorer: points, tier, CO2 avoided, EV savings, donation share vs target, landfill diversion."""
    earned = sum(e["points"] for e in ledger if e["points"] > 0)
    balance = sum(e["points"] for e in ledger)
    co2 = round(sum(e["co2_kg"] for e in ledger), 1)
    ev = [r for r in records if "EV" in str(r.get("vehicle", ""))]
    ev_km = sum(r.get("distance_km", 0) * 2 for r in ev)
    meals = sum(r["portions"] for r in records if r["mode"] == "donate")
    kept = sum(r["kg"] for r in records)
    leftover = sum(d["leftover"] for day in window for d in day["dishes"].values())
    share = round(meals / leftover * 100, 1) if leftover else 0.0
    diverted = round(min(100, kept / (leftover * R["portion_kg"]) * 100), 1) if leftover else 0.0
    return {"points": balance, "earned": earned, "tier": tier(earned), "co2_kg": co2, "meals": meals,
            "kept_kg": round(kept, 1), "ev_pickups": len(ev), "ev_km": round(ev_km, 1),
            "ev_saving_rs": round(ev_km * R["ev_saving_rs_per_km"]), "ev_share_pct": ev_share(records, profile),
            "donation_share_pct": share, "donation_target_pct": R["donation_target_pct"],
            "diverted_pct": diverted, "landfill_goal_pct": R["landfill_goal_pct"], "leftover_portions": leftover}


def ev_share(records, profile):
    base = round(sum("EV" in str(r.get("vehicle", "")) for r in records) / len(records) * 100) if records else 0
    return min(100, base + profile.get("ev_bonus_pct", 0))


# ---------------- green upgrade advisor (rule fallback) ----------------
def gaps(profile, sc):
    g = []
    if profile["lpg_cylinders"] >= 5:
        g.append(("electric_cooking", f"{profile['lpg_cylinders']:g} LPG cylinders a month"))
        g.append(("waste_energy", "LPG use could be cut with biogas from food waste"))
    if not profile["solar"]:
        g.append(("renewable", f"no solar; {profile['electricity_units']} grid units a month"))
    if profile["fridge_age"] >= 8 or profile["electricity_units"] > 1500:
        g.append(("efficiency", f"{profile['fridge_age']}-year-old fridge, high electricity use"))
    if sc["ev_share_pct"] < 50:
        g.append(("ev", f"only {sc['ev_share_pct']}% of pickups are electric"))
    if profile["water_kl"] > 40:
        g.append(("water", f"{profile['water_kl']} kL water a month"))
    g.append(("cold_chain", "fridge temperatures are not monitored"))
    return g


def fits(o, budget):
    return o["price"] * (1 + R["gst_pct"] / 100) <= budget and o["payback"] <= R["max_payback_months"]


def recommend(profile, sc, avoid=()):
    """Top 3 upgrades from different categories that fit the budget, best CO2 and payback first."""
    cats = [c for c, _ in gaps(profile, sc)]
    left, picks = profile["budget"], []
    ranked = sorted((best_offer(k) for k, p in PRODUCTS.items() if p["category"] in cats and k not in avoid),
                    key=lambda o: (cats.index(PRODUCTS[o["product"]]["category"]), o["payback"] - o["co2"] / 20))
    for o in ranked:
        cat = PRODUCTS[o["product"]]["category"]
        if cat in {PRODUCTS[x]["category"] for x in picks} or not fits(o, left):
            continue
        picks.append(o["product"])
        left -= o["price"] * (1 + R["gst_pct"] / 100)
        if len(picks) == 3:
            break
    return picks


# ---------------- orders and invoices ----------------
def price_lines(lines, balance):
    """Subtotal, Green Points discount (1 point = Rs 1, max 10%), GST 18%, total."""
    subtotal = sum(l["price"] * l["qty"] for l in lines)
    discount = min(int(balance), int(subtotal * R["max_points_discount_pct"] / 100)) if balance > 0 else 0
    taxable = subtotal - discount
    gst = round(taxable * R["gst_pct"] / 100)
    return {"subtotal": subtotal, "discount": discount, "taxable": taxable, "gst": gst, "total": taxable + gst}


def critic(lines, money, budget):
    """Rules: over budget, payback > 36 months, duplicate category, discount > 10%, unknown items."""
    problems, seen = [], set()
    if money["total"] > budget:  # checked first: the most important rule
        problems.append(f"order total Rs {money['total']:,} is over the Rs {budget:,} budget")
    for l in lines:
        if l["product"] not in PRODUCTS or l["vendor"] not in {o["vendor"] for o in offers(l["product"])}:
            problems.append(f"{l['product']} from {l['vendor']} is not in the catalogue")
            continue
        cat = PRODUCTS[l["product"]]["category"]
        if cat in seen:
            problems.append(f"two upgrades from the same category ({CATS[cat]['label']})")
        seen.add(cat)
        if l["payback"] > R["max_payback_months"]:
            problems.append(f"{l['name']} pays back in {l['payback']:g} months, over the {R['max_payback_months']}-month limit")
    if money["subtotal"] and money["discount"] > money["subtotal"] * R["max_points_discount_pct"] / 100:
        problems.append(f"Green Points discount is over {R['max_points_discount_pct']}%")
    return problems


# ---------------- revenue model (mock) ----------------
def revenue(orders, records):
    rv = CFG["revenue"]
    live_comm = round(sum(o["taxable"] for o in orders) * R["commission_pct"] / 100)
    recycled = sum(r["kg"] for r in records if r["mode"] == "recycle")
    streams = [
        {"key": "commission", "label": f"Vendor commission {R['commission_pct']}% (paid by vendors)",
         "rs": live_comm + rv["network_commission_rs"], "live_rs": live_comm},
        {"key": "subscriptions", "label": f"Kitchen subscriptions ({rv['subscribed_kitchens']} x Rs {rv['subscription_rs']:,})",
         "rs": rv["subscribed_kitchens"] * rv["subscription_rs"], "live_rs": 0},
        {"key": "reports", "label": f"CSR / BRSR verified reports ({rv['reports_per_month']} x Rs {rv['report_fee_rs']:,})",
         "rs": rv["reports_per_month"] * rv["report_fee_rs"], "live_rs": 0},
        {"key": "gov", "label": "Government dashboard licence", "rs": rv["gov_licence_rs"], "live_rs": 0},
        {"key": "recycler", "label": f"Recycler commission (Rs {rv['recycler_rs_per_kg']}/kg from Pillar 3)",
         "rs": round((recycled + rv["network_recycled_kg"]) * rv["recycler_rs_per_kg"]), "live_rs": round(recycled * rv["recycler_rs_per_kg"])},
    ]
    total = sum(s["rs"] for s in streams)
    return {"streams": streams, "total_rs": total, "running_cost_rs": rv["running_cost_rs"],
            "self_sufficiency_pct": round(total / rv["running_cost_rs"] * 100), "ngos_free": True}
