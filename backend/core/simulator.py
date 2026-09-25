"""
Simulates a day in the canteen. Real demand comes from truth.csv; what gets
sold depends on what the kitchen cooked. This is how the queue gets new data.
"""
from .data import CALENDAR, DISHES, TRUTH


def simulate_day(day, cooked):
    """cooked: {dish: portions}. Returns the day's record for the queue."""
    truth = TRUTH[day]
    cal = CALENDAR[day]
    dishes = {}
    for dish in DISHES:
        demand, made = truth[dish], cooked.get(dish, 0)
        sold = min(demand, made)
        dishes[dish] = {"prepared": made, "sold": sold, "leftover": made - sold,
                        "stockout": demand > made, "missed": max(0, demand - made)}
    return {"date": day, "label": cal["label"], "note": cal["note"],
            "footfall": truth["footfall"], "dishes": dishes}


def usual_habits(day):
    """What the kitchen would have done without CirKit: cook the same every day."""
    return simulate_day(day, {d: info["usual"] for d, info in DISHES.items()})
