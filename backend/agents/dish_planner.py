"""Dish planner agent: turns the customer forecast into portions to cook per dish."""
import math

from core.data import DISHES

from .common import step

BUFFER = 0.05  # 5% cooking buffer; any surplus goes to Pillar 3 (redistribution)


def run(footfall, pattern):
    plan = {}
    for dish, p in pattern["dishes"].items():
        expected = footfall * p["share"] * p["boost"]
        cook = math.ceil(expected * (1 + BUFFER))
        plan[dish] = {"emoji": DISHES[dish]["emoji"], "expected": round(expected), "cook": cook,
                      "usual": DISHES[dish]["usual"],
                      "why": "raised: sold out on similar days" if p["stockouts"] else "matches learned demand"}
    total = sum(v["cook"] for v in plan.values())
    usual = sum(v["usual"] for v in plan.values())
    yield step("Dish planner", f"Cook {total} portions today (usual habit: {usual}). "
                               f"Includes a {int(BUFFER * 100)}% buffer; any surplus is handed to the redistribution pillar.", "👩‍🍳")
    return plan
