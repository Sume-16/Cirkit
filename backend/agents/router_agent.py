"""
Router agent helpers for Pillar 2: what to do with a safe, at-risk batch.
  use_first     - cooks just use it before newer batches (only for a small leftover)
  chef_special  - a special dish today that uses it up (with a recipe idea)
  staff_meal    - cooked for the kitchen staff's meal
  donate        - raw and sealed, with enough time left: handed to Pillar 3 for an NGO
The LLM version lives in expiry_graph.py; this file is the rule-based fallback and the prompt.
"""
import json

from core import batches
from core.food_safety import ITEMS, RULES


def usage_limit(state, item):
    """Small leftover the cooks can absorb just by using this batch first."""
    return RULES["use_first_max_share"] * batches.daily_usage(state).get(item, 0)


def rules(state, c):
    """Rule-based choice for one candidate. Always respects the allowed actions."""
    ok, q = c["allowed"], c["at_risk_qty"]
    hint = ITEMS[c["item"]]["recipe_hint"]
    if "use_first" in ok and q <= usage_limit(state, c["item"]):
        return {"batch": c["id"], "action": "use_first", "recipe": None,
                "reason": "small leftover; cooks can use it up by picking it first"}
    if "donate" in ok and q >= RULES["min_donate_qty"]:
        return {"batch": c["id"], "action": "donate", "recipe": None,
                "reason": f"sealed with {c['hours_left']:.0f} h left: enough time for an NGO pickup"}
    if "chef_special" in ok:
        return {"batch": c["id"], "action": "chef_special", "recipe": hint,
                "reason": "turn the leftover into today's special"}
    if "staff_meal" in ok:
        return {"batch": c["id"], "action": "staff_meal", "recipe": hint, "reason": "cook it for the staff meal"}
    return {"batch": c["id"], "action": ok[0], "recipe": None, "reason": "only safe option left"}


def prompt(candidates, feedback=None):
    rows = [{"batch": c["id"], "item": c["item"], "category": c["category"], "left_at_expiry": c["at_risk_qty"],
             "unit": c["unit"], "hours_left": c["hours_left"], "opened": bool(c["opened"]),
             "value_rs": c["value_at_risk"], "allowed_actions": c["allowed"],
             "recipe_hint": ITEMS[c["item"]]["recipe_hint"]} for c in candidates]
    return ("You route food in an Indian college canteen that will expire before it is used up. "
            "Safety rules have ALREADY checked these batches; choose ONLY from each batch's allowed_actions.\n"
            f"Batches: {json.dumps(rows)}\n"
            f"Guidance: use_first only if the leftover is tiny; donate needs at least {RULES['min_donate_qty']} units "
            "and gives the most social value; chef_special needs a concrete South Indian recipe idea that uses the item; "
            "staff_meal is for small amounts.\n"
            + (f"A safety critic rejected your last routing: {feedback}\n" if feedback else "")
            + 'Reply JSON: {"decisions": [{"batch": "id", "action": "use_first|chef_special|staff_meal|donate", '
              '"recipe": "dish idea or null", "reason": "one short sentence"}]}')
