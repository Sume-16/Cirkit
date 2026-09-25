"""
Inventory agent: compares today's need with stock and decides what to buy.
- Perishables (shelf life <= 5 days): buy only what today needs. Never stockpile.
- Bulk items: reorder when stock after today would cover less than 2 days; top up to 7 days.
"""
import math

from core.data import INGREDIENTS

from .common import step

LEAD_DAYS, TARGET_DAYS = 2, 7


def packs(qty, pack):
    return math.ceil(qty / pack - 1e-9) * pack if qty > 0 else 0


def run(need, stock):
    lines, total = [], 0
    for item, qty in need.items():
        info, have = INGREDIENTS[item], stock.get(item, 0)
        left = have - qty
        perishable = info["shelf_days"] <= 5
        if perishable:
            order = packs(qty - have, info["pack"])
            status = "Buy fresh" if order else "In stock"
        else:
            if left < qty * LEAD_DAYS:
                order = packs(qty * TARGET_DAYS - max(left, 0), info["pack"])
                status = "Short today" if left < 0 else "Reorder bulk"
            else:
                order, status = 0, f"OK · {have / qty:.0f} days cover"
        cost = round(order * info["price"])
        total += cost
        lines.append({"item": item, "unit": info["unit"], "need": round(qty, 2), "stock": round(have, 2),
                      "order": order, "cost": cost, "status": status, "vendor": info["vendor"],
                      "perishable": perishable})
    to_buy = [l for l in lines if l["order"]]
    yield step("Inventory", f"Checked stock for {len(lines)} items: {len(to_buy)} to buy "
                            f"(Rs {total:,}), {len(lines) - len(to_buy)} already covered.", "📦")
    short = [l["item"] for l in lines if l["status"] == "Short today"]
    if short:
        yield step("Inventory", "Urgent, not enough for today: " + ", ".join(short) + ".", "⚠️")
    return lines, total
