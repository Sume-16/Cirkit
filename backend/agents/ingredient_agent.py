"""Ingredient agent: converts portions into raw ingredients using each dish's recipe."""
from core.data import DISHES, INGREDIENTS

from .common import step


def run(plan):
    need = {k: 0.0 for k in INGREDIENTS}
    for dish, p in plan.items():
        for item, per_portion in DISHES[dish]["bom"].items():
            need[item] += p["cook"] * per_portion
    need = {k: round(v, 2) for k, v in need.items() if v > 0}
    yield step("Ingredient", f"Recipe breakdown done: {len(need)} raw ingredients needed for today's cooking.", "🧂")
    return need
