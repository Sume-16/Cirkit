"""
Batch scanner + safety gate tools for Pillar 2 (plain rules, no LLM).
- scan: reads every tagged batch, its countdown, FEFO order and value at risk.
- gate: splits batches into (a) expired / too late -> discard, (b) at risk but fully used
  by FEFO cooking -> "use first", (c) forecast to expire with a leftover (even if it still
  looks fresh today: early warning) -> needs the router agent.
"""
from core import batches

from .common import step


def rs(x):
    return f"Rs {round(x):,}"


def scan(state):
    view = batches.view(state)
    count = {s: sum(v["status"] == s for v in view) for s in ("expired", "critical", "at_risk", "fresh")}
    var = sum(v["value_at_risk"] for v in view)
    yield step("Batch scanner", f"Clock {batches.now(state):%d %b %H:%M}. Scanned {len(view)} tagged batches: "
                                f"{count['expired']} expired, {count['critical']} critical, {count['at_risk']} at risk, "
                                f"{count['fresh']} fresh.", "🏷️")
    yield step("FEFO projector", f"Simulated the next cooking days first-expired-first-out: {rs(var)} of food "
                                 "would still be left when it expires (value at risk).", "⏳")
    return view


def gate(view):
    discard = [v for v in view if v["allowed"] == ["discard"]]
    safe = [v for v in view if v["allowed"] != ["discard"]]
    covered = [v for v in safe if v["status"] in ("critical", "at_risk") and v["at_risk_qty"] <= 0]
    candidates = [v for v in safe if v["at_risk_qty"] > 0]  # includes "fresh" ones forecast to expire unused
    if discard:
        yield step("Safety gate", "Rules only, no AI: " + ", ".join(v["id"] for v in discard)
                   + " cannot be served safely. Set aside for compost/biogas (Pillar 3).", "🚫")
    if covered:
        yield step("Safety gate", ", ".join(v["id"] for v in covered)
                   + " will be fully used by normal cooking before expiry. FEFO: use first.", "🥇")
    if candidates:
        yield step("Safety gate", f"{len(candidates)} safe at-risk batch(es) need a decision: "
                   + ", ".join(f"{v['id']} ({v['at_risk_qty']} {v['unit']}, {v['hours_left']:.0f} h)" for v in candidates)
                   + ". Only these, with their allowed actions, go to the AI router.", "🛡️")
    else:
        yield step("Safety gate", "No at-risk leftovers need routing right now.", "🛡️")
    return discard, covered, candidates
