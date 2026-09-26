"""
Pillar 3 orchestrator: food posts from the kitchen, the redistribution graph, the follow-up agent,
accept / decline by NGOs, recycle pickup requests, the quick-reply chat with its auto-answer bot,
and the records Pillar 4 will use (no carbon maths yet).

A donation counts as VERIFIED for Pillar 4 when the NGO sends "Food collected ✅" in the chat.
"""
import re
from datetime import datetime, timedelta

from core import leftover_safety as ls

from . import rescue_graph
from .common import collect, step

ACTIVE = ("notified", "accepted", "on_the_way")
TAKEN = ("accepted", "on_the_way", "picked_up")
ASK = "Is the food still available?"
COLLECTED = "Food collected ✅"
QUICK = {"kitchen": ["Food is ready for pickup", "Please come in 15 min", "Food no longer available"],
         "ngo": [ASK, "On the way", "Reached", "Running late", COLLECTED]}
FIELDS = ("kitchen", "institution", "dish", "unit", "diet", "storage", "packaging", "address", "contact", "phone", "mode")


def hm(t):
    return t[11:16]


def short(name):
    return name.replace(" (mock)", "")


def find(state, post_id):
    p = next((p for p in state.rescue_posts if p["id"] == post_id), None)
    if p is None:
        raise ValueError(f"No food post called {post_id}.")
    return p


def mark(post, stage, note=""):
    post["timeline"].append({"stage": stage, "at": ls.iso(ls.now()), "note": note})


def say(post, who, text):
    post["chat"].append({"from": who, "text": text, "at": ls.iso(ls.now())})


# ---------------- posting ----------------
def parse_form(f):
    """Validate the kitchen's manual form. Returns a clean post (not saved yet)."""
    f = {k: v for k, v in dict(f).items() if v is not None}
    if f.get("mode") == "recycle":  # how it was kept matters less for waste; oil has no diet
        f["storage"] = f.get("storage") or "room"
        if f.get("recycle_kind") == "used_oil":
            f["diet"] = f.get("diet") or "veg"
    miss = [k for k in FIELDS if not str(f.get(k) or "").strip()]
    if miss or not f.get("cooked_at"):
        raise ValueError("Please fill in: " + ", ".join(miss + ([] if f.get("cooked_at") else ["cooked at"])).replace("_", " ") + ".")
    if f["institution"] not in ls.CFG["institution_types"] or f["storage"] not in ls.RULES["storage"]:
        raise ValueError("Choose an institution type and how the food is kept.")
    if f["mode"] not in ("donate", "recycle") or f["diet"] not in ("veg", "nonveg") or f["unit"] not in ("portions", "kg", "litres"):
        raise ValueError("Invalid choice in the form.")
    if f["mode"] == "recycle" and f.get("recycle_kind") not in ls.KINDS:
        raise ValueError("Choose what kind of food is being recycled.")
    if f["mode"] == "donate" and f["unit"] == "litres":
        raise ValueError("Donations are counted in portions or kg.")
    try:
        qty = float(f["qty"])
    except (TypeError, ValueError, KeyError):
        qty = 0
    if not 0 < qty <= 5000:
        raise ValueError("Enter a quantity greater than 0.")
    phone = re.sub(r"\D", "", str(f["phone"]))[-10:]
    if len(phone) != 10:
        raise ValueError("Enter a 10-digit phone number.")
    now = ls.now()
    t = str(f["cooked_at"])  # "HH:MM" today; a time later than now means last night
    try:
        cooked = datetime.fromisoformat(t) if "T" in t else datetime.combine(now.date(), datetime.strptime(t, "%H:%M").time())
    except ValueError:
        raise ValueError("Enter the cooked-at time as HH:MM.")
    if cooked > now + timedelta(minutes=5):
        cooked -= timedelta(days=1)
    return {**{k: str(f[k]).strip()[:120] for k in FIELDS}, "qty": round(qty, 2), "phone": phone,
            "cooked_at": ls.iso(min(cooked, now)), "spoiled": bool(f.get("spoiled")),
            "recycle_kind": f.get("recycle_kind") if f["mode"] == "recycle" else None}


def precheck(state, f):
    """Live safety preview for the form, plus how every nearby partner fits (same rules as the graph)."""
    if not f.get("cooked_at") or not f.get("storage") and f.get("mode") != "recycle":
        return {"ok": False, "error": "Enter the cooked-at time and how the food is kept to see its safe window."}
    keep = ("cooked_at", "storage", "spoiled", "mode", "diet", "recycle_kind")
    preview = {"kitchen": "-", "institution": "canteen", "dish": "-", "qty": 1, "unit": "portions", "diet": "veg",
               "packaging": "-", "address": "-", "contact": "-", "phone": "0000000000", "mode": "donate",
               "recycle_kind": "cooked_unfit", **{k: f[k] for k in keep if f.get(k)}}
    try:
        if float(f.get("qty") or 0) > 0 and f.get("unit") in ("portions", "kg", "litres"):
            preview["qty"], preview["unit"] = f["qty"], f["unit"]
    except (TypeError, ValueError):
        pass
    try:
        post = parse_form(preview)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    t = ls.now()
    return {"ok": True, **ls.check(post, t), **dict(zip(("kg", "portions"), ls.amounts(post))),
            "fit": {n: ls.fit(post, ls.NGOS[n], t, cap_left(state, n)) for n in ls.NGOS},
            "matching": [p["id"] for p in ls.matching_recyclers(post, t)] if post["mode"] == "recycle" else []}


def create(state, f):
    post = parse_form(f)
    v = ls.check(post, ls.now())
    if post["mode"] == "donate" and not v["donate_ok"]:
        return {"blocked": True, "switch_to": "recycle", "recycle_kind": "cooked_unfit", **v,
                "msg": f"We can't share this with people: {v['reason']}. No worries, we've switched it to "
                       "Recycle so it still becomes animal feed or biogas instead of landfill. 💚"}
    state.rescue_seq += 1
    post.update({"id": f"F{state.rescue_seq}", "status": "posted", "partner": None, "route": None, "plan": None,
                 "message": None, "offered_at": None, "declined": [], "timeline": [], "chat": [], "trace": [],
                 "flagged": False, "attempts": 0, "source": "", "picked_up_at": None, "verified": False})
    mark(post, "posted", f"{post['kitchen']} posted {post['dish']}")
    state.rescue_posts.insert(0, post)
    state.save()  # saved before any agent runs, so it survives a refresh
    return {"blocked": False, "post": view(state, post)}


# ---------------- graph runs ----------------
def cap_left(state, ngo_id, skip=None):
    today = ls.now().date().isoformat()
    used = sum(ls.amounts(p)[1] for p in state.rescue_posts if p["partner"] == ngo_id and p["id"] != skip
               and p["mode"] == "donate" and p["status"] in TAKEN and p["timeline"][0]["at"].startswith(today))
    return max(0, ls.NGOS[ngo_id]["capacity"] - used)


def pending_recyclers(state, skip=None):
    return [p["partner"] for p in state.rescue_posts
            if p["mode"] == "recycle" and p["status"] == "notified" and p["id"] != skip and p["partner"]]


def graph(state, post, exclude=(), stress=False):
    return rescue_graph.run(post, lambda n: cap_left(state, n, post["id"]), exclude, stress, pending_recyclers(state, post["id"]))


def apply(post, s):
    lot = s["lot"]
    if lot["mode"] != post["mode"]:
        post["mode"], post["recycle_kind"] = lot["mode"], lot["recycle_kind"]
    mark(post, "safety", s["safety"]["reason"])
    choice = s.get("choice")
    post.update({"attempts": s["attempts"], "source": s.get("source", ""), "flagged": s.get("flagged", False)})
    if choice not in ls.PARTNERS:
        post.update({"status": "unmatched", "partner": None, "route": None, "plan": None})
        mark(post, "unmatched", "landfill is the last resort" if choice == "landfill" else "no partner can take it safely")
        return
    p = ls.PARTNERS[choice]
    post.update({"status": "notified", "partner": choice, "route": s["route"], "plan": s.get("plan"),
                 "message": s["message"], "offered_at": ls.iso(ls.now())})
    mark(post, "matched", p["name"])


def run(state, post_id, stress=False):
    post = find(state, post_id)
    if post["status"] != "posted":
        raise ValueError(f"{post_id} has already been matched.")
    gen = graph(state, post, (), stress)
    while True:  # stream the steps live and keep them on the post
        try:
            ev = next(gen)
        except StopIteration as done:
            s = done.value
            break
        post["trace"].append(ev)
        yield ev
    apply(post, s)
    state.save()
    yield {"type": "result", "post": view(state, post)}


def follow_up(state, post, why):
    """Follow-up agent: the NGO declined or did not reply, so offer the food to the next best NGO."""
    old = ls.PARTNERS[post["partner"]]["name"]
    post["declined"].append(post["partner"])
    mark(post, "declined", f"{short(old)} {why}")
    trace = [step("Follow-up", f"{old} {why}. Offering {post['dish']} to the next best NGO.", "📨")]
    t, s = collect(graph(state, post, post["declined"]))
    trace += t
    apply(post, s)
    if post["status"] == "notified":
        mark(post, "reassigned", short(ls.PARTNERS[post["partner"]]["name"]))
        say(post, "bot", f"🔄 {short(old)} {why}. CirKit offered the food to {short(ls.PARTNERS[post['partner']]['name'])}.")
    post["trace"] += trace
    return trace


def check_timeouts(state):
    t, changed = ls.now(), False
    for post in state.rescue_posts:
        if (post["status"] == "notified" and post["mode"] == "donate" and post["offered_at"]
                and t - datetime.fromisoformat(post["offered_at"]) >= timedelta(minutes=ls.RULES["follow_up_minutes"])):
            follow_up(state, post, f"did not accept within {ls.RULES['follow_up_minutes']} min")
            changed = True
    if changed:
        state.save()


def decline(state, post_id, ngo_id=None, demo=False):
    post = find(state, post_id)
    if post["status"] != "notified" or post["mode"] != "donate":
        raise ValueError("Only a food offer waiting for an NGO can be declined.")
    if ngo_id and ngo_id != post["partner"]:
        raise ValueError(f"This food is offered to {short(ls.PARTNERS[post['partner']]['name'])}, not this NGO.")
    trace = follow_up(state, post, "declined (demo)" if demo else "declined")
    state.save()
    return {"trace": trace, "post": view(state, post)}


# ---------------- accept, pickup requests, collection ----------------
def accept(state, post_id, ngo_id):
    """An NGO accepts. The offered NGO, or any other NGO that passes every rule, may take it."""
    post = find(state, post_id)
    if post["mode"] != "donate" or post["status"] not in ("notified",):
        raise ValueError("This food is not open for accepting any more.")
    if ngo_id not in ls.NGOS:
        raise ValueError("Unknown NGO.")
    t = ls.now()
    if ngo_id != post["partner"]:
        problem = ls.check_ngo(post, ls.NGOS[ngo_id], t, cap_left(state, ngo_id, post_id))
        if problem:
            raise ValueError(f"Can't accept: {problem}.")
        post.update({"partner": ngo_id, "route": ls.route(ls.NGOS[ngo_id], t)})
        if post.get("plan"):
            post["plan"] = {**post["plan"], "chosen": ngo_id, "km": post["route"]["km"], "eta_min": post["route"]["eta_min"],
                            "margin_min": ls.margin_min(post, ls.NGOS[ngo_id], t), "reason": "accepted directly by this NGO"}
    post["status"] = "accepted"
    mark(post, "accepted", short(ls.NGOS[ngo_id]["name"]))
    say(post, "bot", f"✅ {short(ls.NGOS[ngo_id]['name'])} accepted. Call them on {ls.NGOS[ngo_id]['phone']} (demo number).")
    state.save()
    return {"post": view(state, post)}


def request_pickup(state, post_id, partner_id):
    """Kitchen sends a recycle pickup request to a specific partner (rules re-check it)."""
    post = find(state, post_id)
    if post["mode"] != "recycle" or post["status"] not in ("notified", "unmatched"):
        raise ValueError("Only an open recycle post can get a pickup request.")
    p = ls.RECYCLERS.get(partner_id)
    problem = ls.check_recycler(post, p, ls.now()) if p else "unknown partner"
    if problem:
        raise ValueError(f"Can't send: {problem}.")
    t = ls.now()
    stops = {x: ls.PARTNERS[x] for x in pending_recyclers(state, post_id)}
    stops[partner_id] = p
    post.update({"partner": partner_id, "status": "notified", "route": ls.route(p, t),
                 "plan": {"kind": "recycle", "chosen": partner_id, **ls.nearest_neighbour(stops.values())}})
    mark(post, "matched", f"pickup request to {short(p['name'])}")
    state.save()
    return {"post": view(state, post)}


def collected(state, post, by):
    post["status"], post["picked_up_at"], post["verified"] = "picked_up", ls.iso(ls.now()), True
    mark(post, "picked_up", by)
    p, (kg, portions) = ls.PARTNERS[post["partner"]], ls.amounts(post)
    state.rescue_records.append({  # what Pillar 4 needs later; no carbon credits here
        "post": post["id"], "mode": post["mode"], "dish": post["dish"], "kg": kg,
        "portions": portions if post["mode"] == "donate" else 0, "destination": p["type"],
        "recycle_kind": post["recycle_kind"], "partner": p["name"], "distance_km": post["route"]["km"],
        "vehicle": p["vehicle"], "posted_at": post["timeline"][0]["at"], "picked_up_at": post["picked_up_at"],
        "verified": True, "verified_by": by})


def mark_collected(state, post_id):
    """Recycle only. Donations are verified by the NGO's "Food collected ✅" message."""
    post = find(state, post_id)
    if post["mode"] != "recycle" or post["status"] != "notified":
        raise ValueError("Donations are confirmed when the NGO sends \"Food collected ✅\" in the chat.")
    collected(state, post, "kitchen confirmed recycle pickup")
    state.save()
    return {"post": view(state, post)}


# ---------------- chat ----------------
def chat(state, post_id, sender, text):
    post = find(state, post_id)
    text = " ".join(str(text).split())[:200]
    if sender not in QUICK or not text:
        raise ValueError("Empty message.")
    if not post["partner"] or post["mode"] != "donate":
        raise ValueError("Chat opens once an NGO has been offered the food.")
    say(post, sender, text)
    left = ls.minutes_left(post, ls.now())
    if sender == "ngo" and "still available" in text.lower():  # auto-answer bot: live data, no LLM
        kg, portions = ls.amounts(post)
        if post["status"] in ACTIVE and left > 0:
            say(post, "bot", f"🤖 Yes, {portions} portions of {post['dish']}, safe for {int(left)} more minutes "
                             f"(consume by {hm(ls.iso(ls.consume_by(post)))}).")
        else:
            say(post, "bot", "🤖 No, it has been " + ("collected." if post["status"] == "picked_up"
                                                     else "withdrawn by the kitchen." if post["status"] == "cancelled" else "expired."))
    elif sender == "ngo" and text == "On the way" and post["status"] in ("notified", "accepted"):
        if post["status"] == "notified":
            accept(state, post_id, post["partner"])
        post["status"] = "on_the_way"
        mark(post, "on_the_way", short(ls.PARTNERS[post["partner"]]["name"]))
    elif sender == "ngo" and text == COLLECTED and post["status"] in ACTIVE:
        if post["status"] == "notified":
            accept(state, post_id, post["partner"])
        collected(state, post, f"NGO chat: {COLLECTED}")
        say(post, "bot", "📦 Picked up and verified. Thank you for not wasting food!")
    elif sender == "kitchen" and text == "Food no longer available" and post["status"] in ACTIVE:
        post["status"] = "cancelled"
        mark(post, "cancelled", "kitchen withdrew the food")
    state.save()
    return {"post": view(state, post)}


# ---------------- state for the website ----------------
def view(state, post):
    t = ls.now()
    kg, portions = ls.amounts(post)
    p = ls.PARTNERS.get(post["partner"])
    out = {**post, "kg": kg, "portions": portions, "consume_by": ls.iso(ls.consume_by(post)),
           "secs_left": round(ls.minutes_left(post, t) * 60),
           "partner_info": {k: p[k] for k in ("id", "name", "type", "phone", "vehicle")} if p else None}
    if post["mode"] == "donate" and post["status"] in ("posted",) + ACTIVE:
        out["fit"] = {n: ls.fit(post, ls.NGOS[n], t, cap_left(state, n, post["id"])) for n in ls.NGOS}
    if post["mode"] == "recycle":
        out["matching"] = [x["id"] for x in ls.matching_recyclers(post, t)]
    return out


def totals(state):
    rec = state.rescue_records
    by = {k: round(sum(r["kg"] for r in rec if r["destination"] == k), 1) for k in ("animal_feed", "biogas", "compost", "biodiesel")}
    return {"meals_donated": sum(r["portions"] for r in rec if r.get("verified")),
            "verified_donations": sum(1 for r in rec if r["mode"] == "donate" and r.get("verified")),
            "kg_donated": round(sum(r["kg"] for r in rec if r["mode"] == "donate" and r.get("verified")), 1),
            "recycled_kg": by, "kept_from_landfill_kg": round(sum(r["kg"] for r in rec if r["destination"] != "landfill"), 1),
            "active": sum(p["status"] in ACTIVE for p in state.rescue_posts), "posts": len(state.rescue_posts)}


def partners(state):
    """Mock NGOs and recycle partners with distance and ETA from the kitchen, nearest first."""
    eta = lambda p: ls.RULES["dispatch_minutes"] + ls.trip_min(p)
    ngos = [{**p, "km": ls.road_km(p), "eta_min": eta(p), "cap_left": cap_left(state, p["id"])} for p in ls.NGOS.values()]
    recyclers = [{**p, "km": ls.road_km(p), "eta_min": eta(p), "label": ls.DEST[p["type"]]["label"],
                  "emoji": ls.DEST[p["type"]]["emoji"]} for p in ls.RECYCLERS.values()]
    by_km = lambda p: p["km"]
    return {"kitchen": ls.KITCHEN, "ngos": sorted(ngos, key=by_km), "recyclers": sorted(recyclers, key=by_km)}


def summary(state):
    check_timeouts(state)
    pend = pending_recyclers(state)
    run = ls.nearest_neighbour([ls.RECYCLERS[x] for x in dict.fromkeys(pend)]) if pend else None
    return {"now": ls.iso(ls.now()), **partners(state),
            "posts": [view(state, p) for p in state.rescue_posts], "records": state.rescue_records,
            "recycle_run": run, "totals": totals(state), "quick": QUICK, "institution_types": ls.CFG["institution_types"],
            "recycle_kinds": {k: v["label"] for k, v in ls.KINDS.items()}, "storage": ls.RULES["storage"],
            "destinations": ls.DEST, "rules": {k: ls.RULES[k] for k in ("follow_up_minutes", "min_donate_minutes", "serve_buffer_minutes",
                                                                          "road_factor", "city_speed_kmph")},
            "about": ls.CFG["_about"]}
