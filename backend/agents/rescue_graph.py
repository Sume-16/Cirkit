"""
CirKit Pillar 3 (Redistribution network) as a LangGraph agent graph.

  safety -> matcher -> router -> critic --(bad match, retry)--> matcher
                                       +--(approved)--> outreach -> dispatch -> END

- safety:   RULES ONLY. Consume-by time from cooked-at + how it is kept. Unsafe "donate" food is
            switched to recycle. The LLM never decides safety.
- matcher:  Donate: Llama picks an NGO using tool results (distance, capacity, veg-only).
            Recycle: the recovery ladder picks the right partner (animal feed, biogas, compost, oil).
- router:   route optimisation. Donate: ranks every NGO that arrives before consume-by with room,
            and keeps a backup. Recycle: one nearest-neighbour pickup run through all pending partners.
            Estimated route: haversine x 1.3 road factor, 20 km/h city speed.
- critic:   rejects late arrival, over capacity, diet mismatch, unsafe food to people, wrong recycle
            partner, or a far NGO when a nearer one works. Feedback goes back to the matcher
            (max 3 tries, then rules + flag).
- outreach: Llama writes a short pickup message; template fallback.
- dispatch: offers the food. The follow-up agent re-runs this graph if the NGO declines.
Every agent falls back to rules if the LLM is unavailable, so the demo never breaks.
"""
from typing import List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from core import leftover_safety as ls

from .common import call_llm, llm_model, step
from .schemas import MatchOutput, OutreachOutput, parse

MAX_ATTEMPTS = ls.RULES["max_attempts"]


class RescueState(TypedDict, total=False):
    lot: dict
    safety: dict
    ladder: list
    choice: Optional[str]
    reason: str
    source: str
    route: Optional[dict]
    plan: Optional[dict]
    attempts: int
    feedback: Optional[str]
    approved: bool
    flagged: bool
    message: Optional[str]
    trace: List[dict]


def hm(t):
    return (t if isinstance(t, str) else ls.iso(t))[11:16]


def short(name):
    return name.replace(" (mock)", "")


def build(post, cap_left, exclude=(), stress=False, pending=()):
    """cap_left(ngo_id) -> people the NGO can still feed today. exclude: NGOs that declined.
    pending: recycle partners already waiting for a pickup (joined into one run).
    stress=True makes the first match deliberately bad so judges can watch the critic catch it."""
    t = ls.now()

    def feasible(lot):
        """NGOs that pass every rule, nearest first."""
        return sorted((p for p in ls.NGOS.values() if p["id"] not in exclude
                       and not ls.check_ngo(lot, p, t, cap_left(p["id"]))), key=ls.road_km)

    def safety(s: RescueState):
        lot = dict(s["lot"])
        v = ls.check(lot, t)
        trace = [step("Safety check", f"Cooked {hm(lot['cooked_at'])}, {ls.storage_label(lot)}"
                                      f" → consume by {hm(v['consume_by'])} ({max(v['minutes_left'], 0)} min left). Rules only, no LLM.", "🛡️")]
        if lot["mode"] == "donate" and not v["donate_ok"]:
            lot["mode"], lot["recycle_kind"] = "recycle", lot.get("recycle_kind") or "cooked_unfit"
            trace.append(step("Safety check", f"Blocked for people: {v['reason']}. Switched to RECYCLE.", "🚫"))
        elif lot["mode"] == "donate":
            trace.append(step("Safety check", f"Safe to donate: {v['reason']}.", "✅"))
        ladder = []
        if lot["mode"] == "recycle":
            ladder, why = ls.recycle_types(lot, t)
            trace.append(step("Safety check", f"Recovery ladder for {why}: "
                              + " → ".join(ls.DEST[x]["label"] for x in ladder) + ".", "♻️"))
        return {"lot": lot, "safety": v, "ladder": ladder, "attempts": 0, "feedback": None, "trace": trace}

    def matcher(s: RescueState):
        lot, n = s["lot"], s["attempts"] + 1
        kg, portions = ls.amounts(lot)
        trace = [step("Matcher", f"Attempt {n}: finding {'an NGO' if lot['mode'] == 'donate' else 'a recycle partner'}"
                      + (f" with the critic's feedback: \"{s['feedback']}\"" if s.get("feedback") else "") + ".", "🎯")]
        if lot["mode"] == "recycle":
            opts = [p for p in ls.matching_recyclers(lot, t) if p["id"] not in exclude]
            if opts:
                p = opts[0]
                choice, reason = p["id"], f"{ls.DEST[p['type']]['label'].lower()} is the best step on the ladder; nearest partner, collects {p['days']}"
            else:
                choice, reason = "landfill", "no recycle partner can take it"
            source = "Rules (recovery ladder)"
            if stress and n == 1:
                p = min(ls.NGOS.values(), key=ls.road_km)
                choice, reason, source = p["id"], "stress test: deliberately sends unsafe food to people", "Stress test"
            name = ls.PARTNERS[choice]["name"] if choice in ls.PARTNERS else "Landfill"
            trace.append(step("Matcher", f"{source} → {name}. {reason.rstrip('.')}.", "🧭"))
            return {"choice": choice, "reason": reason, "source": source, "attempts": n, "trace": trace}

        ngos = [p for p in ls.NGOS.values() if p["id"] not in exclude]
        if not ngos:
            trace.append(step("Matcher", "Every nearby NGO has already declined this post.", "😔"))
            return {"choice": None, "reason": "no NGO left", "source": "Rules", "attempts": n, "trace": trace}
        facts = [{"p": p, "route": ls.route(p, t), "fit": ls.fit(lot, p, t, cap_left(p["id"]))} for p in ngos]
        if n == 1:
            kms = [f["route"]["km"] for f in facts]
            veg = [f for f in facts if f["p"]["veg_only"]]
            trace += [step("Distance tool", f"{len(facts)} NGOs, {min(kms)}–{max(kms)} km by road (haversine × 1.3, 20 km/h).", "📏"),
                      step("Capacity tool", f"{sum(f['fit']['cap_ok'] for f in facts)} of {len(facts)} NGOs can still feed "
                                            f"{portions} people today.", "👥"),
                      step("Diet tool", (f"{len(veg)} veg-only NGO(s) cannot take this non-veg dish." if lot["diet"] != "veg"
                                         else "Veg dish: every NGO can take it."), "🥗"),
                      step("Time tool", f"{sum(f['fit']['in_time'] for f in facts)} of {len(facts)} NGOs can get it there "
                                        f"before the consume-by time ({hm(s['safety']['consume_by'])}).", "⏱️")]
        lines = "\n".join(f"- {f['p']['id']}: {f['p']['name']} ({f['p']['kind']}), {f['route']['km']} km by road, "
                          f"food arrives {f['fit']['margin_min']} min before consume-by, room for {f['fit']['cap_left']} people, "
                          f"veg-only {'yes' if f['p']['veg_only'] else 'no'}" for f in facts)
        out = parse(MatchOutput, call_llm(
            f"A {lot['institution']} is donating {portions} portions ({kg} kg) of {lot['dish']} ({lot['diet']}), "
            f"{ls.storage_label(lot)}. It must be eaten by {hm(s['safety']['consume_by'])}.\nNGOs (tool results):\n{lines}\n"
            f"Rules: food must arrive at least {ls.RULES['serve_buffer_minutes']} min before consume-by; the NGO needs room for "
            "all portions; veg-only NGOs cannot take non-veg; choose the NEAREST NGO that meets every rule.\n"
            + (f"A critic rejected your last choice: {s['feedback']}\n" if s.get("feedback") else "")
            + 'Reply JSON: {"partner": "ngo id", "reason": "one short sentence"}'))
        ids = {f["p"]["id"] for f in facts}
        if out and out.partner in ids:
            choice, reason, source = out.partner, out.reason, f"Llama ({llm_model()})"
        else:
            ok = feasible(lot)
            choice = ok[0]["id"] if ok else None
            reason = "nearest NGO that arrives in time and has room" if ok else "no NGO passes the rules"
            source = "Rules (no LLM key or invalid JSON)"
        if stress and n == 1:
            far = max(facts, key=lambda f: f["route"]["km"])
            choice, reason, source = far["p"]["id"], "stress test: deliberately picks the farthest NGO", "Stress test"
        if choice:
            trace.append(step("Matcher", f"{source} → {ls.NGOS[choice]['name']}. {reason.rstrip('.')}.", "🧭"))
        return {"choice": choice, "reason": reason, "source": source, "attempts": n, "trace": trace}

    def router(s: RescueState):
        p = ls.PARTNERS.get(s.get("choice"))
        if not p:
            return {"route": None, "plan": None, "trace": []}
        r = ls.route(p, t)
        if s["lot"]["mode"] == "donate" and p["type"] == "ngo":
            ranked = [x["id"] for x in feasible(s["lot"])]
            backup = next((i for i in ranked if i != p["id"]), None)
            m = ls.margin_min(s["lot"], p, t)
            plan = {"kind": "donate", "chosen": p["id"], "ranked": ranked, "backup": backup, "km": r["km"],
                    "eta_min": r["eta_min"], "margin_min": m, "reason": s.get("reason", "")}
            top = ", ".join(f"{k + 1}. {short(ls.NGOS[i]['name'])} {ls.road_km(ls.NGOS[i])} km" for k, i in enumerate(ranked[:3]))
            msg = (f"Ranked {len(ranked)} NGO(s) that arrive in time with room: {top or 'none'}. "
                   f"{short(p['name'])}: {r['km']} km, pickup in {r['eta_min']} min, food arrives {hm(r['arrive_at'])}, "
                   f"{m} min before consume-by." + (f" Backup: {short(ls.NGOS[backup]['name'])}." if backup else ""))
            return {"route": r, "plan": plan, "trace": [step("Router", msg, "🗺️")]}
        stops = {x: ls.PARTNERS[x] for x in pending if x in ls.RECYCLERS}
        stops[p["id"]] = p
        run = ls.nearest_neighbour(stops.values())
        plan = {"kind": "recycle", "chosen": p["id"], **run}
        order = " → ".join(short(l["name"]) for l in run["legs"])
        note = ""
        if s["lot"].get("recycle_kind") == "used_oil" and s["lot"]["unit"] == "litres" and s["lot"]["qty"] < ls.RULES["oil_min_litres"]:
            note = f" Collector needs at least {ls.RULES['oil_min_litres']} L: store the oil until then."
        return {"route": r, "plan": plan, "trace": [step("Router", f"One pickup run (nearest-neighbour): Kitchen → {order}, "
                                                                   f"{run['total_km']} km, about {run['minutes']} min.{note}", "🗺️")]}

    def problem_of(s, choice):
        p = ls.PARTNERS[choice]
        if s["lot"]["mode"] == "donate":
            bad = ls.check_ngo(s["lot"], p, t, cap_left(choice))
            if bad:
                return bad
            best = feasible(s["lot"])  # route optimisation: don't drive past a nearer NGO that works
            if best and ls.road_km(p) - ls.road_km(best[0]) > ls.RULES["optimise_slack_km"]:
                return (f"{short(best[0]['name'])} is {ls.road_km(p) - ls.road_km(best[0]):.1f} km nearer than "
                        f"{short(p['name'])} and also arrives in time with room")
            return None
        return ls.check_recycler(s["lot"], p, t)

    def fallback(s):
        """Rules pick the nearest partner that passes every check (used after 3 tries)."""
        pool = [p for p in ls.PARTNERS.values() if p["id"] not in exclude
                and (p["type"] == "ngo") == (s["lot"]["mode"] == "donate")]
        ok = sorted((p for p in pool if not problem_of(s, p["id"])), key=ls.road_km)
        return ok[0]["id"] if ok else None

    def critic(s: RescueState):
        choice = s.get("choice")
        if choice is None:
            return {"approved": True, "flagged": True, "trace": [step("Critic", "No NGO can take this food safely right now. "
                    "Flagged for the kitchen: split the post or switch it to recycle.", "⚠️")]}
        if choice == "landfill":
            return {"approved": True, "flagged": True, "trace": [step("Critic", "No recycle partner fits. Landfill as the last resort, flagged.", "🗑️")]}
        problem = problem_of(s, choice)
        if not problem:
            what = "nearest that arrives in time, has room, diet matches" if s["lot"]["mode"] == "donate" else "right partner for this waste"
            return {"approved": True, "flagged": False, "feedback": None,
                    "trace": [step("Critic", f"Match approved: {ls.PARTNERS[choice]['name']} ({what}).", "✅")]}
        if s["attempts"] < MAX_ATTEMPTS:
            return {"approved": False, "feedback": problem,
                    "trace": [step("Critic", f"Rejected: {problem}. Sending it back to the matcher.", "🔁")]}
        safe = fallback(s)
        p = ls.PARTNERS.get(safe)
        out = {"approved": True, "flagged": True, "choice": safe, "source": "Rules (critic override)",
               "trace": [step("Critic", f"Still wrong after {MAX_ATTEMPTS} tries. "
                                        + (f"Rules chose {p['name']} instead" if p else "No partner passes the rules")
                                        + ", flagged for the kitchen.", "🛑")]}
        if p:  # re-plan the route for the override
            upd = router({**s, "choice": safe})
            out.update({"route": upd["route"], "plan": upd["plan"]})
            out["trace"] += upd["trace"]
        return out

    def after_critic(s: RescueState):
        return "outreach" if s["approved"] else "matcher"

    def outreach(s: RescueState):
        lot, p, r = s["lot"], ls.PARTNERS.get(s.get("choice")), s.get("route")
        if not p or not r:
            return {"message": None, "trace": []}
        kg, portions = ls.amounts(lot)
        what = (f"{portions} portions ({kg} kg) of {lot['dish']} ({lot['diet']})" if lot["mode"] == "donate"
                else f"{kg} kg of {ls.KINDS[lot['recycle_kind']]['label'].lower()}")
        template = (f"Namaste {short(p['name'])}! {lot['kitchen']} has {what} ready for pickup at {lot['address']}. "
                    + (f"Please eat by {hm(s['safety']['consume_by'])}. " if lot["mode"] == "donate" else f"You collect: {p['days']}. ")
                    + f"Pickup by {hm(r['pickup_at'])}. Call {lot['contact']} on {lot['phone']}. Thank you! - CirKit")
        out = parse(OutreachOutput, call_llm(
            f"Write a short, warm WhatsApp pickup message (max 55 words) to {short(p['name'])} from {lot['kitchen']}. "
            f"Food: {what}. Pickup address: {lot['address']}. Pickup by {hm(r['pickup_at'])}. "
            + (f"Must be eaten by {hm(s['safety']['consume_by'])}. " if lot["mode"] == "donate" else "")
            + f"Contact {lot['contact']} on {lot['phone']}. Sign off as CirKit. Reply JSON: {{\"message\": \"...\"}}"))
        msg, by = (out.message, f"Llama ({llm_model()})") if out else (template, "Template (no LLM key)")
        return {"message": msg, "trace": [step("Outreach", f"{by} wrote the pickup message for {p['name']}.", "📣")]}

    def dispatch(s: RescueState):
        p = ls.PARTNERS.get(s.get("choice"))
        if not p:
            msg = "Nothing dispatched." + (" Landfill logged as last resort." if s.get("choice") == "landfill" else "")
        elif s["lot"]["mode"] == "donate":
            msg = (f"Offered to {p['name']}. Waiting for them to accept; the follow-up agent offers it to the next "
                   f"best NGO if they decline or don't reply within {ls.RULES['follow_up_minutes']} min.")
        else:
            msg = f"Pickup request sent to {p['name']} ({p['days']}). Hand it over and mark it collected."
        return {"trace": [step("Dispatch", msg, "📦")]}

    g = StateGraph(RescueState)
    for name, fn in [("safety", safety), ("matcher", matcher), ("router", router), ("critic", critic),
                     ("outreach", outreach), ("dispatch", dispatch)]:
        g.add_node(name, fn)
    g.add_edge(START, "safety")
    g.add_edge("safety", "matcher")
    g.add_edge("matcher", "router")
    g.add_edge("router", "critic")
    g.add_conditional_edges("critic", after_critic, {"matcher": "matcher", "outreach": "outreach"})
    g.add_edge("outreach", "dispatch")
    g.add_edge("dispatch", END)
    return g.compile()


def run(post, cap_left, exclude=(), stress=False, pending=()):
    """Stream the graph: yields trace steps live, returns the final state."""
    final = {"lot": dict(post)}
    yield step("LangGraph", f"Starting the redistribution agent graph for {post['id']} ({post['dish']}).", "🕸️")
    for update in build(post, cap_left, exclude, stress, pending).stream(dict(final), stream_mode="updates"):
        for node, changes in update.items():
            for t in changes.get("trace", []):
                yield t
            final.update(changes)
    return final
