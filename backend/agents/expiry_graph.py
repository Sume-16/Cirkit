"""
CirKit Pillar 2 (Expiry & batch tracking) as a LangGraph agent graph.

  scan -> safety_gate -> router -> critic --(unsafe routing, retry)--> router
                                        +--(approved)--> dispatch -> END
                                                        (then the manager applies it on the website)

- scan:        tool reads every tagged batch, countdowns and the FEFO projection (value at risk).
- safety_gate: RULES ONLY. Expired or too-late food -> discard. The LLM never decides safety.
- router:      Llama picks use first / chef's special (with a recipe idea) / staff meal / donate,
               only for batches the rules marked safe, and only from their allowed actions.
               Its JSON is validated with Pydantic.
- critic:      rule-based safety critic re-checks every choice. Unsafe -> feedback -> router (max 3).
- dispatch:    turns decisions into jobs for the chef, the store keeper and Pillar 3 (donations).
Every agent falls back to rules if the LLM is unavailable, so the demo never breaks.
"""
from typing import List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from core import batches
from core import food_safety as fs

from . import batch_agent, router_agent
from .common import call_llm, collect, llm_model, step
from .schemas import RouterOutput, parse

MAX_ATTEMPTS = 3


class ExpiryState(TypedDict, total=False):
    clock: str
    view: list
    discard: list
    covered: list
    candidates: list
    decisions: list
    source: str
    attempts: int
    feedback: Optional[str]
    approved: bool
    flagged: bool
    jobs: list
    trace: List[dict]


def build(kitchen, stress=False):
    """stress=True makes the router's first answer deliberately unsafe so judges can watch
    the safety critic catch it and loop back."""

    def scan(s: ExpiryState):
        t, view = collect(batch_agent.scan(kitchen))
        return {"view": view, "attempts": 0, "feedback": None, "trace": t}

    def safety_gate(s: ExpiryState):
        t, (discard, covered, candidates) = collect(batch_agent.gate(s["view"]))
        return {"discard": discard, "covered": covered, "candidates": candidates, "trace": t}

    def router(s: ExpiryState):
        n, cands = s["attempts"] + 1, s["candidates"]
        sabotage = stress and n == 1 and (cands or s["discard"])
        if not cands and not sabotage:
            return {"decisions": [], "source": "Rules", "attempts": n, "trace": []}
        trace = [step("Router", f"Attempt {n}: deciding what to do with {len(cands)} at-risk batch(es)"
                      + (f" with the critic's feedback: \"{s['feedback']}\"" if s.get("feedback") else "") + ".", "🧭")]
        out = parse(RouterOutput, call_llm(router_agent.prompt(cands, s.get("feedback")))) if cands else None
        rule = {c["id"]: router_agent.rules(kitchen, c) for c in cands}
        llama = f"Llama ({llm_model()})"
        if out:
            decisions = [{**d.model_dump(), "by": llama} for d in out.decisions]
            got = {d["batch"] for d in decisions}
            missing = [i for i in rule if i not in got]
            decisions += [{**rule[i], "by": "Rules (gap fill)"} for i in missing]  # LLM skipped a batch
            source = llama + (" + rules" if missing else "")
        else:
            source = "Rules (no LLM key or invalid JSON)"
            decisions = [{**d, "by": source} for d in rule.values()]
        if sabotage:  # deliberately unsafe first answer: serve expired food, or break a rule
            bad = s["discard"][0] if s["discard"] else cands[0]
            wrong = "chef_special" if bad in s["discard"] else next(
                (a for a in fs.ACTIONS if a not in bad["allowed"]), "chef_special")
            decisions = [d for d in decisions if d["batch"] != bad["id"]] + [
                {"batch": bad["id"], "action": wrong, "recipe": None, "by": "Stress test",
                 "reason": "stress test: deliberately unsafe routing"}]
        for d in decisions:
            trace.append(step("Router", f"{d['by']}: {d['batch']} → {d['action'].replace('_', ' ')}"
                              + (f" ({d['recipe']})" if d.get("recipe") else "") + f". {d['reason'].rstrip('.')}.", "🍽️"))
        return {"decisions": decisions, "source": source, "attempts": n, "trace": trace}

    def problems_in(s):
        by_id = {v["id"]: v for v in s["view"]}
        cand_ids = {c["id"] for c in s["candidates"]}
        problems, seen = [], set()
        for d in s["decisions"]:
            b = by_id.get(d["batch"])
            if b is None:
                problems.append(f"{d['batch']} is not a real batch")
                continue
            if d["batch"] in seen:
                problems.append(f"{d['batch']} was routed twice")
            seen.add(d["batch"])
            p = fs.check(d["action"], b)
            if p:
                problems.append(p)
            elif d["batch"] not in cand_ids:
                problems.append(f"{d['batch']} was not sent for routing")
            elif (d["action"] == "use_first" and len(b["allowed"]) > 1
                  and b["at_risk_qty"] > router_agent.usage_limit(kitchen, b["item"])):
                problems.append(f"use first leaves {b['at_risk_qty']} {b['unit']} of {d['batch']} to expire anyway")
            elif d["action"] == "donate" and b["at_risk_qty"] < fs.RULES["min_donate_qty"]:
                problems.append(f"{b['at_risk_qty']} {b['unit']} of {d['batch']} is too little for an NGO pickup")
            elif d["action"] == "chef_special" and not (d.get("recipe") or "").strip():
                problems.append(f"chef's special for {d['batch']} has no recipe idea")
        for c in s["candidates"]:
            if c["id"] not in seen:
                problems.append(f"{c['id']} has no decision")
        return problems

    def critic(s: ExpiryState):
        if not s["candidates"] and not s["decisions"]:
            return {"approved": True, "flagged": False,
                    "trace": [step("Safety critic", "Nothing to route. FEFO order and discards follow the rules.", "✅")]}
        problems = problems_in(s)
        if not problems:
            return {"approved": True, "flagged": False, "feedback": None,
                    "trace": [step("Safety critic", f"All {len(s['decisions'])} routing decisions pass the "
                                                    "food-safety rules (FSSAI-guided).", "✅")]}
        if s["attempts"] < MAX_ATTEMPTS:
            return {"approved": False, "feedback": problems[0],
                    "trace": [step("Safety critic", f"Rejected: {problems[0]}. Sending it back to the router.", "🔁")]}
        safe = [{**router_agent.rules(kitchen, c), "by": "Rules (critic override)"} for c in s["candidates"]]
        return {"approved": True, "flagged": True, "decisions": safe, "feedback": None, "source": "Rules (critic override)",
                "trace": [step("Safety critic", f"Still unsafe after {MAX_ATTEMPTS} tries. Replaced with the "
                                                "rule-based routing and flagged for the manager.", "🛑")]}

    def after_critic(s: ExpiryState):
        return "dispatch" if s["approved"] else "router"

    def dispatch(s: ExpiryState):
        by_id = {v["id"]: v for v in s["view"]}
        jobs = []
        for v in s["discard"]:
            jobs.append({"batch": v["id"], "item": v["item"], "action": "discard", "qty": v["qty"], "unit": v["unit"],
                         "value": v["value"], "hours_left": v["hours_left"], "recipe": None, "source": "Safety rules",
                         "reason": "expired" if v["status"] == "expired" else "too little time left to handle safely"})
        for v in s["covered"]:
            jobs.append({"batch": v["id"], "item": v["item"], "action": "use_first", "qty": 0, "unit": v["unit"],
                         "value": 0, "hours_left": v["hours_left"], "recipe": None, "source": "FEFO rule",
                         "reason": "normal cooking uses it all before expiry"})
        for d in s["decisions"]:
            v = by_id[d["batch"]]
            jobs.append({"batch": v["id"], "item": v["item"], "action": d["action"], "qty": v["at_risk_qty"],
                         "unit": v["unit"], "value": v["value_at_risk"], "hours_left": v["hours_left"],
                         "recipe": d.get("recipe"), "source": d["by"], "reason": d["reason"]})
        saved = sum(j["value"] for j in jobs if j["action"] != "discard")
        lost = sum(j["value"] for j in jobs if j["action"] == "discard")
        trace = [step("Dispatcher", f"{len(jobs)} job(s) ready: protects Rs {saved:,} of food"
                                    + (f"; Rs {lost:,} already lost to expiry" if lost else "") + ".", "📋"),
                 step("Orchestrator", "Routing plan ready for the kitchen team.", "🧾")]
        return {"jobs": jobs, "trace": trace}

    g = StateGraph(ExpiryState)
    g.add_node("scan", scan)
    g.add_node("safety_gate", safety_gate)
    g.add_node("router", router)
    g.add_node("critic", critic)
    g.add_node("dispatch", dispatch)
    g.add_edge(START, "scan")
    g.add_edge("scan", "safety_gate")
    g.add_edge("safety_gate", "router")
    g.add_edge("router", "critic")
    g.add_conditional_edges("critic", after_critic, {"router": "router", "dispatch": "dispatch"})
    g.add_edge("dispatch", END)
    return g.compile()


def run(kitchen, stress=False):
    """Stream the graph: yields trace steps live, returns the final state."""
    final = {"clock": batches.iso(batches.now(kitchen))}
    yield step("LangGraph", f"Starting the expiry agent graph at {batches.now(kitchen):%d %b %H:%M}.", "🕸️")
    for update in build(kitchen, stress).stream(dict(final), stream_mode="updates"):
        for node, changes in update.items():
            for t in changes.get("trace", []):
                yield t
            final.update(changes)
    return final
