"""Queue agent: reads the rolling 10-day window (FIFO) and summarises it."""
from .common import step


def run(state):
    w = list(state.window)
    labels = {}
    for r in w:
        labels[r["label"]] = labels.get(r["label"], 0) + 1
    mix = ", ".join(f"{n} {lbl}" for lbl, n in labels.items())
    cooked = sum(d["prepared"] for r in w for d in r["dishes"].values())
    sold = sum(d["sold"] for r in w for d in r["dishes"].values())
    outs = sum(d["stockout"] for r in w for d in r["dishes"].values())
    yield step("Queue", f"Rolling window holds {len(w)} days: {w[0]['date']} to {w[-1]['date']} ({mix}).", "🗂️")
    yield step("Queue", f"In this window the kitchen cooked {cooked} portions and sold {sold}: "
                        f"{(cooked - sold) / cooked * 100:.0f}% left over, and dishes sold out {outs} times.", "📉")
    return w
