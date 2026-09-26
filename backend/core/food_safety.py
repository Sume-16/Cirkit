"""
Food-safety rules for Pillar 2. FSSAI-guided (not FSSAI-certified) assumptions from data/shelf_life.json.
This file is the ONLY place that decides if food is safe. It is plain rules, no LLM.
The LLM router only ever sees batches these rules already marked safe, with the actions
they allow, and the safety critic re-checks every LLM choice against these same rules.
"""
from .data import load_json

SHELF = load_json("shelf_life.json")
RULES, ITEMS = SHELF["rules"], SHELF["items"]
ACTIONS = ["use_first", "chef_special", "staff_meal", "donate"]


def life_hours(item, opened):
    return ITEMS[item]["opened_hours" if opened else "sealed_hours"]


def status(hours_left, total_hours):
    """expired < critical < at_risk < fresh."""
    if hours_left <= 0:
        return "expired"
    if hours_left <= RULES["critical_hours"]:
        return "critical"
    if hours_left <= min(RULES["at_risk_hours"], RULES["at_risk_life_fraction"] * total_hours):
        return "at_risk"
    return "fresh"


def donate_blocked(item, opened):
    return bool(opened) and ITEMS[item]["category"] in RULES["never_donate_opened"]


def allowed_actions(b):
    """Actions that are safe for this batch view. Expired food, or food with too little time
    left to handle safely, can only be discarded (to compost/biogas via Pillar 3)."""
    if b["status"] == "expired":
        return ["discard"]
    ok = [a for a in ACTIONS if b["hours_left"] >= RULES["min_hours"][a]]
    if donate_blocked(b["item"], b["opened"]):
        ok = [a for a in ok if a != "donate"]
    return ok or ["discard"]


def check(action, b):
    """Returns a problem sentence if the action is unsafe for this batch, else None."""
    if b["status"] == "expired" and action != "discard":
        return f"{b['id']} has expired; it can only be discarded (compost/biogas), never {action.replace('_', ' ')}"
    if action == "donate" and donate_blocked(b["item"], b["opened"]):
        return f"{b['id']} is opened {ITEMS[b['item']]['category']}; opened high-risk food is never donated raw"
    if action in RULES["min_hours"] and b["hours_left"] < RULES["min_hours"][action]:
        return (f"{b['id']} has only {b['hours_left']:.0f} h left; {action.replace('_', ' ')} needs at least "
                f"{RULES['min_hours'][action]} h")
    if action not in ACTIONS + ["discard"]:
        return f"'{action}' is not a known action"
    if action not in allowed_actions(b):
        return f"{action.replace('_', ' ')} is not allowed for {b['id']} (allowed: {', '.join(allowed_actions(b))})"
    return None
