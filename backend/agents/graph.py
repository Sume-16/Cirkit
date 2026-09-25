"""
CirKit Pillar 1 as a LangGraph agent graph.

  observe -> forecaster -> planner -> critic --(bad plan, retry)--> forecaster
                                          +--(approved)--> inventory -> END
                                                            (then the manager approves on the website)

- observe:    tools read the rolling 10-day queue, learn the day-type pattern and past bias.
- forecaster: Llama (via Groq) decides tomorrow's customers from the evidence and explains why.
- planner:    turns customers into portions per dish and raw ingredients (tool maths).
- critic:     hard safety rules + Llama review. If the plan is bad it sends feedback back
              to the forecaster (max 3 attempts). This is the self-correction loop.
- inventory:  compares need with stock and drafts the purchase order.
Every agent falls back to rules if the LLM is unavailable, so the demo never breaks.
"""
from typing import Any, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from core.data import CALENDAR

from . import (context_agent, dish_planner, ingredient_agent, inventory_agent, learning_agent,
               pattern_agent, queue_agent)
from .common import call_llm, clamp, collect, llm_model, step

MAX_ATTEMPTS = 3


class PlanState(TypedDict, total=False):
    day: str
    label: str
    note: str
    window: list
    pattern: dict
    baseline: float
    bias: float
    safe_low: float
    safe_high: float
    footfall: float
    reason: str
    source: str
    attempts: int
    feedback: Optional[str]
    approved: bool
    flagged: bool
    dishes: dict
    need: dict
    lines: list
    total: int
    trace: List[dict]


def build(kitchen, stress=False):
    """Build the graph for one planning run. stress=True makes the first forecast
    deliberately wrong so judges can watch the critic catch it and loop back."""

    def observe(s: PlanState):
        t1, window = collect(queue_agent.run(kitchen))
        t2, pattern = collect(pattern_agent.run(window, s["label"]))
        t3, bias = collect(learning_agent.run(kitchen.history))
        mean = sum(r["footfall"] for r in window) / len(window)
        return {"window": window, "pattern": pattern, "baseline": pattern["footfall"], "bias": bias,
                "safe_low": 0.3 * mean, "safe_high": 1.8 * mean, "attempts": 0, "feedback": None,
                "trace": t1 + t2 + t3}

    def forecaster(s: PlanState):
        n = s["attempts"] + 1
        base = s["baseline"] * s["bias"]
        trace = [step("Forecaster", f"Attempt {n}: reasoning over the evidence"
                      + (f" with the critic's feedback: \"{s['feedback']}\"" if s.get("feedback") else "") + ".", "🧠")]
        out = call_llm(
            f"Tomorrow ({s['day']}) is a '{s['label']}' day. Calendar note: \"{s['note'] or 'none'}\".\n"
            f"Evidence from our rolling 10-day window: recency-weighted baseline for this day type is "
            f"{s['baseline']:.0f} customers; learning correction from past errors is x{s['bias']:.2f} "
            f"(so {base:.0f}). The day type is ALREADY in the baseline; only adjust for the note.\n"
            + (f"A critic rejected your last forecast: {s['feedback']}\n" if s.get("feedback") else "")
            + 'Decide the number of customers. Reply JSON: {"footfall": integer, "reason": "one short sentence"}')
        if out and isinstance(out.get("footfall"), (int, float)):
            footfall, reason, source = float(out["footfall"]), str(out.get("reason", "")), f"Llama ({llm_model()})"
        else:
            ctx = context_agent.rules(s["note"], base)
            footfall, reason, source = base * ctx["factor"], ctx["reason"], "Rules (no LLM key)"
        if stress and n == 1:
            footfall, reason = footfall * 2.2, "stress test: deliberately bad first guess"
        trace.append(step("Forecaster", f"{source}: {footfall:.0f} customers. {reason.rstrip('.')}.", "📈"))
        return {"footfall": footfall, "reason": reason, "source": source, "attempts": n, "trace": trace}

    def planner(s: PlanState):
        t1, dishes = collect(dish_planner.run(s["footfall"], s["pattern"]))
        t2, need = collect(ingredient_agent.run(dishes))
        return {"dishes": dishes, "need": need, "trace": t1 + t2}

    def critic(s: PlanState):
        f, lo, hi, base = s["footfall"], s["safe_low"], s["safe_high"], s["baseline"] * s["bias"]
        problems = []
        if not lo <= f <= hi:
            problems.append(f"{f:.0f} is outside the safe range {lo:.0f}-{hi:.0f} for this kitchen")
        elif abs(f - base) / base > (0.5 if s["note"] else 0.3):
            problems.append(f"{f:.0f} is {abs(f - base) / base * 100:.0f}% away from the evidence ({base:.0f}), "
                            "more than the calendar note can justify")
        if not problems:  # rules passed, now Llama reviews the reasoning
            review = call_llm(
                f"Review a canteen forecast. Day type '{s['label']}', note \"{s['note'] or 'none'}\", evidence "
                f"{base:.0f} customers, forecast {f:.0f}, reason: \"{s['reason']}\". Approve unless it is clearly "
                'unreasonable. Reply JSON: {"approve": true or false, "feedback": "one short sentence"}')
            if review and review.get("approve") is False:
                problems.append(str(review.get("feedback", "LLM critic disagreed")))
        if not problems:
            return {"approved": True, "flagged": False, "feedback": None,
                    "trace": [step("Critic", f"Plan approved: {f:.0f} customers is consistent with the evidence.", "✅")]}
        if s["attempts"] < MAX_ATTEMPTS:
            return {"approved": False, "feedback": problems[0],
                    "trace": [step("Critic", f"Rejected: {problems[0]}. Sending it back to the forecaster.", "🔁")]}
        fixed = clamp(f, lo, hi)
        return {"approved": True, "flagged": True, "footfall": fixed, "feedback": None,
                "trace": [step("Critic", f"Still wrong after {MAX_ATTEMPTS} tries. Limited to {fixed:.0f} "
                                         "and flagged for the manager.", "🛑")]}

    def after_critic(s: PlanState):
        if s["approved"]:
            return "inventory"
        return "forecaster"

    def inventory(s: PlanState):
        dishes, need = s["dishes"], s["need"]
        trace = []
        if s.get("flagged"):  # footfall was limited, so re-plan portions once
            t1, dishes = collect(dish_planner.run(s["footfall"], s["pattern"]))
            t2, need = collect(ingredient_agent.run(dishes))
            trace += t1 + t2
        t3, (lines, total) = collect(inventory_agent.run(need, kitchen.stock))
        trace += t3 + [step("Orchestrator", "Plan ready. Waiting for the manager's approval before buying.", "🧾")]
        return {"dishes": dishes, "need": need, "lines": lines, "total": total, "trace": trace}

    g = StateGraph(PlanState)
    g.add_node("observe", observe)
    g.add_node("forecaster", forecaster)
    g.add_node("planner", planner)
    g.add_node("critic", critic)
    g.add_node("inventory", inventory)
    g.add_edge(START, "observe")
    g.add_edge("observe", "forecaster")
    g.add_edge("forecaster", "planner")
    g.add_edge("planner", "critic")
    g.add_conditional_edges("critic", after_critic, {"forecaster": "forecaster", "inventory": "inventory"})
    g.add_edge("inventory", END)
    return g.compile()


def run(kitchen, stress=False):
    """Stream the graph: yields trace steps live, returns the final state."""
    day = kitchen.next_date
    cal = CALENDAR[day]
    final = {"day": day, "label": cal["label"], "note": cal["note"]}
    yield step("LangGraph", f"Starting the agent graph for {day}.", "🕸️")
    for update in build(kitchen, stress).stream(dict(final), stream_mode="updates"):
        for node, changes in update.items():
            for t in changes.get("trace", []):
                yield t
            final.update(changes)
    return final
