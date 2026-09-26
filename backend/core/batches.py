"""
Pillar 2 batch ledger. Every delivery becomes a tagged batch (e.g. "Curd B3") with its own
received time, sealed shelf life and, once opened, a shorter opened shelf life.
Stock is used FEFO: First Expired, First Out.

Demo clock: the kitchen day starts at 07:00 on the day being planned (state.next_date).
"Advance clock" adds hours inside that day; closing the day moves the clock 24 h forward.
"""
from datetime import datetime, timedelta

from . import food_safety as fs
from .data import DISHES, INGREDIENTS

HORIZON_DAYS = 7
# Opening stock is assumed to have arrived a while ago (hours before day 1), so the demo
# starts with a realistic mix of fresh and nearly expired batches.
SEED_AGE_HOURS = {"Tomato": 62, "Curd": 44, "Coriander & mint": 30, "Mixed vegetables": 40, "Green chilli": 80,
                  "Onion": 150, "Potato": 190, "Ginger-garlic paste": 640, "Wheat flour": 900}
SEED_OPENED = {"Ginger-garlic paste": 120}  # jar opened 120 h before day 1


def now(state):
    return datetime.fromisoformat(state.next_date + "T07:00") + timedelta(hours=state.clock_h)


def iso(t):
    return t.isoformat(timespec="minutes")


def new_batch(state, item, qty, received, opened=None, source="order"):
    n = state.batch_seq.get(item, 0) + 1
    state.batch_seq[item] = n
    b = {"id": f"{item} B{n}", "item": item, "qty": round(qty, 2), "received": iso(received),
         "opened": iso(opened) if opened else None, "source": source, "priority": False}
    state.batches.append(b)
    return b


def seed(state):
    state.batches, state.batch_seq = [], {}
    t0 = now(state)
    for item, qty in state.stock.items():
        if qty > 0:
            received = t0 - timedelta(hours=SEED_AGE_HOURS.get(item, 240))
            opened = t0 - timedelta(hours=SEED_OPENED[item]) if item in SEED_OPENED else None
            new_batch(state, item, qty, received, opened, "opening stock")


def expires_at(b):
    info = fs.ITEMS[b["item"]]
    t = datetime.fromisoformat(b["received"]) + timedelta(hours=info["sealed_hours"])
    if b["opened"]:
        t = min(t, datetime.fromisoformat(b["opened"]) + timedelta(hours=info["opened_hours"]))
    return t


def receive(state, item, qty, source="order"):
    return new_batch(state, item, qty, now(state), None, source)


def _drop_empty(state):
    state.batches = [b for b in state.batches if b["qty"] > 0.001]


def consume_fefo(state, item, qty):
    """Cooking (at cook_hour) uses the batch that expires first, never an expired one. Returns [(id, used)]."""
    t = datetime.fromisoformat(state.next_date) + timedelta(hours=fs.RULES["cook_hour"])
    used = []
    for b in sorted((b for b in state.batches if b["item"] == item and expires_at(b) > t),
                    key=lambda b: (not b["priority"], expires_at(b))):
        if qty <= 0:
            break
        take = min(b["qty"], qty)
        b["qty"] = round(b["qty"] - take, 3)
        qty -= take
        used.append((b["id"], round(take, 2)))
    _drop_empty(state)
    return used


def take(state, batch_id, qty):
    """Remove qty from one batch (chef special, staff meal, donation, discard). Keeps Pillar 1 stock in sync."""
    b = next(b for b in state.batches if b["id"] == batch_id)
    qty = min(qty, b["qty"])
    b["qty"] = round(b["qty"] - qty, 3)
    state.stock[b["item"]] = round(max(0, state.stock.get(b["item"], 0) - qty), 2)
    _drop_empty(state)
    return round(qty, 2)


def daily_usage(state):
    """Average use per ingredient per day, from what was actually cooked in the 10-day window."""
    days = len(state.window)
    use = {}
    for r in state.window:
        for dish, d in r["dishes"].items():
            for item, q in DISHES[dish]["bom"].items():
                use[item] = use.get(item, 0) + q * d["prepared"] / days
    return use


def project(state):
    """FEFO projection: simulate the next cooking days and see how much of each batch
    will still be left when it expires. That leftover is the quantity at risk."""
    t, avg = now(state), daily_usage(state)
    today = avg
    if state.plan and state.plan["date"] == state.next_date:
        today = {l["item"]: l["need"] for l in state.plan["ingredients"]}
    day0 = datetime.fromisoformat(state.next_date) + timedelta(hours=fs.RULES["cook_hour"])
    left = {b["id"]: b["qty"] for b in state.batches}
    for k in range(HORIZON_DAYS):
        cook = day0 + timedelta(days=k)
        use = today if k == 0 else avg
        for item, need in use.items():
            for b in sorted((b for b in state.batches if b["item"] == item and expires_at(b) > max(cook, t)),
                            key=lambda b: (not b["priority"], expires_at(b))):
                if need <= 0:
                    break
                x = min(left[b["id"]], need)
                left[b["id"]] -= x
                need -= x
    horizon = day0 + timedelta(days=HORIZON_DAYS)
    return {b["id"]: (round(left[b["id"]], 2) if expires_at(b) <= horizon else 0.0) for b in state.batches}


def view(state):
    """Batches with countdown, safety status, FEFO rank and value at risk, most urgent first."""
    t, risk = now(state), project(state)
    out = []
    for b in state.batches:
        info, ing = fs.ITEMS[b["item"]], INGREDIENTS[b["item"]]
        exp = expires_at(b)
        hours = (exp - t).total_seconds() / 3600
        total = fs.life_hours(b["item"], b["opened"])
        st = fs.status(hours, total)
        at_risk = b["qty"] if st == "expired" else risk[b["id"]]  # predictive: even "fresh" batches
        v = {**b, "unit": ing["unit"], "price": ing["price"], "value": round(b["qty"] * ing["price"]),
             "expires_at": iso(exp), "hours_left": round(hours, 1),
             "life_pct": max(0, min(100, round(hours / total * 100))), "status": st,
             "category": info["category"], "storage": info["storage"],
             "at_risk_qty": round(at_risk, 2), "value_at_risk": round(at_risk * ing["price"])}
        v["allowed"] = fs.allowed_actions(v)
        out.append(v)
    rank = {}
    for v in sorted(out, key=lambda v: (not v["priority"], v["expires_at"])):
        if v["status"] == "expired":  # never picked: it waits for discard
            v["fefo_rank"] = None
            continue
        rank[v["item"]] = rank.get(v["item"], 0) + 1
        v["fefo_rank"] = rank[v["item"]]
    return sorted(out, key=lambda v: v["hours_left"])
