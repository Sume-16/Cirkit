"""
Pattern agent: learns demand from the window, per day label.
- Footfall: recency-weighted average of days with the same label.
- If the window has no day with that label yet, it scales normal days by a prior ratio.
- Dish demand: each dish's share of footfall on similar days.
- Sold-out dishes: sales hide the true demand, so the agent adds a correction.
"""
import math

from .common import step

PRIOR = {"normal": 1.0, "weekend": 0.65, "festival": 1.4, "exam": 1.08, "holiday": 0.45}


def weighted_avg(values):
    weights = range(1, len(values) + 1)  # newer days count more
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def run(window, target_label):
    similar = [r for r in window if r["label"] == target_label]
    if similar:
        footfall = weighted_avg([r["footfall"] for r in similar])
        yield step("Pattern", f"Found {len(similar)} '{target_label}' day(s) in the window. "
                              f"Recency-weighted footfall: {footfall:.0f} customers.", "🔍")
    else:
        normal = [r for r in window if r["label"] == "normal"] or window
        footfall = weighted_avg([r["footfall"] for r in normal]) * PRIOR[target_label]
        yield step("Pattern", f"No '{target_label}' day in the current window. Using normal days "
                              f"x{PRIOR[target_label]} prior ratio: {footfall:.0f} customers.", "🧭")
    basis = similar or window
    total_ff = sum(r["footfall"] for r in basis)
    dishes = {}
    for dish in window[0]["dishes"]:
        sold = sum(r["dishes"][dish]["sold"] for r in basis)
        outs = sum(r["dishes"][dish]["stockout"] for r in basis)
        boost = 1 + min(0.12, 0.03 * outs)
        dishes[dish] = {"share": sold / total_ff, "stockouts": outs, "boost": boost}
    flagged = [f"{d} ({v['stockouts']}x)" for d, v in dishes.items() if v["stockouts"]]
    if flagged:
        yield step("Pattern", "Sold out on similar days: " + ", ".join(flagged) +
                              ". Sales under-count real demand there, so I raise those dishes.", "🔥")
    top = max(dishes, key=lambda d: dishes[d]["share"])
    yield step("Pattern", f"Most popular: {top} ({dishes[top]['share'] * 100:.0f}% of customers order it).", "⭐")
    return {"footfall": footfall, "dishes": dishes, "similar_days": len(similar)}
