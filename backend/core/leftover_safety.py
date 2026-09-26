"""
Pillar 3 food-safety and logistics rules for COOKED food posted by a kitchen. FSSAI-guided
(not FSSAI-certified) assumptions from data/redistribution.json. This file is the ONLY place
that decides whether food may go to people. It is plain rules, never the LLM.

  Donate  -> people (NGOs, shelters, community kitchens), only inside the safe window.
  Recycle -> the recovery ladder: animal feed -> compost / biogas, used oil -> biodiesel.
             Landfill is the last resort.
Routes are ESTIMATED: haversine x 1.3 road factor at an average city speed of 20 km/h.
The LLM matcher only chooses WHICH partner; the critic re-checks every choice with these rules.
"""
import math
from datetime import datetime, timedelta, timezone

from .data import load_json

CFG = load_json("redistribution.json")
RULES, KITCHEN = CFG["rules"], CFG["kitchen"]
NGOS = {p["id"]: {**p, "type": "ngo"} for p in CFG["ngos"]}
RECYCLERS = {p["id"]: p for p in CFG["recyclers"]}
PARTNERS = {**NGOS, **RECYCLERS}
KINDS, DEST = CFG["recycle_kinds"], CFG["destinations"]
IST = timezone(timedelta(hours=5, minutes=30))


def now():
    """Kitchen wall clock in India time (naive, so it compares with the form's times)."""
    return datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)


def iso(t):
    return t.isoformat(timespec="minutes")


# ---------------- safety window ----------------
def window_hours(storage):
    return RULES["storage"][storage]["safe_hours"]


def storage_label(post):
    s = RULES["storage"][post["storage"]]["label"]
    return s[0].lower() + s[1:]


def consume_by(post):
    return datetime.fromisoformat(post["cooked_at"]) + timedelta(hours=window_hours(post["storage"]))


def minutes_left(post, t):
    return (consume_by(post) - t).total_seconds() / 60


def check(post, t):
    """Safety check agent's verdict for a post. Donate only if clearly safe for people."""
    left, label = minutes_left(post, t), storage_label(post)
    base = {"consume_by": iso(consume_by(post)), "minutes_left": round(left), "window_h": window_hours(post["storage"])}
    if post.get("spoiled"):
        return {**base, "donate_ok": False, "reason": "it was marked as looking spoiled, so it must not reach people"}
    if left <= 0:
        return {**base, "donate_ok": False,
                "reason": f"{label} food is safe for {window_hours(post['storage']):g} h after cooking, and that window has passed"}
    if left < RULES["min_donate_minutes"]:
        return {**base, "donate_ok": False,
                "reason": f"only {left:.0f} min are left before the consume-by time, too little for a safe pickup "
                          f"(needs at least {RULES['min_donate_minutes']} min)"}
    return {**base, "donate_ok": True, "reason": f"{label}, safe to eat for {left / 60:.1f} more h"}


# ---------------- quantities ----------------
def amounts(post):
    """(kg, portions) for any unit the kitchen typed."""
    q, u = post["qty"], post["unit"]
    if u == "portions":
        return round(q * RULES["portion_kg"], 2), int(q)
    kg = q * RULES["oil_kg_per_litre"] if u == "litres" else q
    return round(kg, 2), (int(kg / RULES["portion_kg"]) if post["mode"] == "donate" else 0)


# ---------------- distance and time tools (estimated) ----------------
def road_km_between(a, b):
    """Haversine distance x 1.3 road factor."""
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(h)) * RULES["road_factor"], 1)


def road_km(p):
    return road_km_between(KITCHEN, p)


def drive_min(km):
    return round(km / RULES["city_speed_kmph"] * 60)


def trip_min(p):
    return drive_min(road_km(p))


def route(p, t):
    """Partner's vehicle leaves now, reaches the kitchen, and carries the food back."""
    trip = trip_min(p)
    pickup = t + timedelta(minutes=RULES["dispatch_minutes"] + trip)
    return {"km": road_km(p), "trip_min": trip, "vehicle": p["vehicle"],
            "pickup_at": iso(pickup), "arrive_at": iso(pickup + timedelta(minutes=trip)),
            "eta_min": RULES["dispatch_minutes"] + trip}


def margin_min(post, p, t):
    """Minutes between the food reaching the NGO and its consume-by time."""
    return round((consume_by(post) - datetime.fromisoformat(route(p, t)["arrive_at"])).total_seconds() / 60)


def fit(post, p, t, cap_left):
    """What the Who's-nearby card shows for one NGO and one food post."""
    m = margin_min(post, p, t)
    _, portions = amounts(post)
    return {"in_time": m >= RULES["serve_buffer_minutes"], "margin_min": m, "cap_ok": cap_left >= portions,
            "diet_ok": not (p["veg_only"] and post["diet"] != "veg"), "cap_left": cap_left}


def nearest_neighbour(stops):
    """One multi-stop pickup run from the kitchen, always driving to the nearest unvisited stop."""
    here, left, legs = KITCHEN, list(stops), []
    while left:
        nxt = min(left, key=lambda s: road_km_between(here, s))
        legs.append({"id": nxt["id"], "name": nxt["name"], "km": road_km_between(here, nxt), "lat": nxt["lat"], "lon": nxt["lon"]})
        left.remove(nxt)
        here = nxt
    total = round(sum(l["km"] for l in legs), 1)
    return {"legs": legs, "total_km": total, "minutes": drive_min(total)}


# ---------------- recycle ladder ----------------
def recycle_types(post, t):
    """Allowed partner types for a recycle post, best first, with the reason."""
    kind, age_h = post["recycle_kind"], (t - datetime.fromisoformat(post["cooked_at"])).total_seconds() / 3600
    ladder = list(KINDS[kind]["ladder"])
    why = KINDS[kind]["label"].lower()
    if "animal_feed" in ladder:
        if post["diet"] != "veg":
            ladder.remove("animal_feed")
            why += ": has meat or egg, never fed to cattle"
        elif post.get("spoiled"):
            ladder.remove("animal_feed")
            why += ": spoiled or moldy, never fed to animals"
        elif kind == "cooked_unfit" and age_h > RULES["animal_feed_max_hours"]:
            ladder.remove("animal_feed")
            why += f": {age_h:.0f} h old, too old for animals"
        else:
            why += ": clean and veg, animals first"
    return ladder, why


def matching_recyclers(post, t):
    """Partners that may take this waste, best ladder step first, then nearest."""
    ladder, _ = recycle_types(post, t)
    ok = [p for p in RECYCLERS.values() if p["type"] in ladder and post["recycle_kind"] in p["takes"]]
    return sorted(ok, key=lambda p: (ladder.index(p["type"]), road_km(p)))


# ---------------- critic checks ----------------
def check_ngo(post, p, t, cap_left):
    """Problem sentence if this partner cannot take this donation, else None."""
    if p["type"] != "ngo":
        return f"{p['name']} is a recycle partner, not people. Safe food should feed people first"
    if not check(post, t)["donate_ok"]:
        return f"this food is not safe for people any more, so it cannot go to {p['name']}"
    if p["veg_only"] and post["diet"] != "veg":
        return f"{p['name']} is veg-only and this dish is non-veg"
    r = route(p, t)
    if r["trip_min"] > RULES["max_transit_minutes"]:
        return (f"{p['name']} is {r['km']} km away: the {r['trip_min']} min trip is longer than the "
                f"{RULES['max_transit_minutes']} min safe transit limit for cooked food")
    if margin_min(post, p, t) < RULES["serve_buffer_minutes"]:
        return (f"food would reach {p['name']} at {r['arrive_at'][11:]}, too close to its consume-by time "
                f"({iso(consume_by(post))[11:]})")
    _, portions = amounts(post)
    if cap_left < portions:
        return f"{p['name']} can feed only {cap_left} more people today, not {portions}"
    return None


def check_recycler(post, p, t):
    if p["type"] == "ngo":
        return f"{p['name']} serves people, and recycle food is not fit for people"
    allowed, _ = recycle_types(post, t)
    if p["type"] not in allowed or post["recycle_kind"] not in p["takes"]:
        return f"{p['name']} ({DEST[p['type']]['label'].lower()}) must not take {KINDS[post['recycle_kind']]['label'].lower()} here"
    return None
