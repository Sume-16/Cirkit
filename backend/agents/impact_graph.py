"""
CirKit Pillar 4 (Revenue & Impact) as a LangGraph agent graph.

  collector -> verifier -> scorer -> advisor -> procurement -> critic --(bad order, retry)--> advisor
                                                                    +--(approved)--> approval || reporter -> END
                                                                                   (graph pauses here until the
                                                                                    manager presses "Approve order")
- collector:   tools read the ledger and Pillars 1-3 (verified pickups, leftovers, EV trips, energy profile).
- verifier:    RULES decide: SHA-256 hash chain intact, only donations confirmed by the NGO's
               "Food collected ✅" count, spikes are flagged. Llama only adds a short note.
- scorer:      Green Credit Points (new ledger entries), tier, CO2 avoided, EV savings, donation share.
- advisor:     Llama reads the energy profile and impact gaps and recommends the top 3 upgrades in budget.
- procurement: Llama + compare tool: best-value vendor per product, Green Points discount (max 10%), draft order.
- critic:      rules: over budget, payback > 36 months, duplicate category, discount > 10% -> advisor (max 3).
- approval:    the graph stops (LangGraph interrupt) until the manager approves in the website.
- reporter:    Llama writes a short ESG summary and a 3-line city summary; template fallback.
Every LLM step validates JSON with Pydantic and falls back to rules, so the demo never breaks.
"""
import uuid
from typing import List, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from core import impact as im
from core import ledger as lg
from core import leftover_safety as ls

from .common import call_llm, llm_model, step
from .schemas import AdvisorOutput, ProcurementOutput, ReporterOutput, VerifierNote, parse

MAX_ATTEMPTS = im.R["max_attempts"]
SAVER = MemorySaver()
GRAPHS = {}  # run id -> compiled graph, so an approval can resume the paused run

FAKE = {"post": "FAKE-1", "mode": "donate", "dish": "Veg biryani", "kg": 144.0, "portions": 480, "destination": "ngo",
        "partner": "Unknown NGO", "distance_km": 0, "vehicle": "bike", "verified": False,
        "verified_by": "self-reported by kitchen", "stress": True}


class ImpactState(TypedDict, total=False):
    records: list
    window: list
    profile: dict
    ledger_ok: bool
    accepted: list
    rejected: list
    scores: dict
    gaps: list
    recs: list
    lines: list
    money: dict
    attempts: int
    feedback: Optional[str]
    approved: bool
    flagged: bool
    report: Optional[dict]
    trace: List[dict]


def rs(n):
    return f"Rs {n:,.0f}"


def report_template(sc, lines):
    esg = (f"Our canteen (demo) holds {sc['points']:,} Green Credit Points ({sc['tier']['name']} tier). Verified pickups fed "
           f"{sc['meals']} meals and kept {sc['kept_kg']} kg of food out of landfill, avoiding about {sc['co2_kg']} kg CO2e "
           f"(mock estimate). Donation share is {sc['donation_share_pct']}% of leftovers against a {sc['donation_target_pct']}% target"
           + (f"; {len(lines)} green upgrade(s) were approved." if lines else "."))
    city = [f"{sc['meals']} verified meals reached partner NGOs; only NGO-confirmed pickups are counted.",
            f"{sc['diverted_pct']}% of leftovers diverted from landfill vs the city's {sc['landfill_goal_pct']}% goal.",
            f"{sc['ev_pickups']} EV pickups saved about {rs(sc['ev_saving_rs'])} in fuel (mock)."]
    return {"esg_summary": esg, "city_summary": city, "by": "Template (no LLM key)"}


def write_report(sc, lines):
    out = parse(ReporterOutput, call_llm(
        f"Write an honest ESG summary (max 80 words) for a college canteen, and a 3-line summary for the city dashboard. "
        f"Facts (mock estimates, call them Green Credit Points, never carbon credits): {sc['points']} points, {sc['tier']['name']} tier, "
        f"{sc['meals']} verified meals donated, {sc['kept_kg']} kg kept from landfill, {sc['co2_kg']} kg CO2e avoided, "
        f"donation share {sc['donation_share_pct']}% vs {sc['donation_target_pct']}% target, landfill diversion {sc['diverted_pct']}% vs "
        f"{sc['landfill_goal_pct']}% goal, {sc['ev_pickups']} EV pickups. Approved upgrades: "
        f"{', '.join(l['name'] for l in lines) or 'none'}. "
        'Reply JSON: {"esg_summary": "...", "city_summary": ["line 1", "line 2", "line 3"]}'))
    return {**out.model_dump(), "by": f"Llama ({llm_model()})"} if out else report_template(sc, lines)


def build(kitchen, stress=False):
    imp = kitchen.green
    t = ls.now()

    def collector(s: ImpactState):
        records = [dict(r) for r in kitchen.rescue_records]
        tr = [step("Ledger tool", f"Read {len(imp['ledger'])} ledger entries (SHA-256 chained).", "🔗"),
              step("Pillar 3 tool", f"Read {len(records)} pickup record(s) from the redistribution network.", "🤝"),
              step("Pillar 1 tool", f"Read the 10-day window: {sum(d['leftover'] for day in kitchen.window for d in day['dishes'].values())} "
                                    "leftover portions to measure donation share.", "📈"),
              step("Profile tool", f"Energy profile: {imp['profile']['lpg_cylinders']:g} LPG cylinders, {imp['profile']['electricity_units']} "
                                   f"units, fridge {imp['profile']['fridge_age']} yrs, solar {'yes' if imp['profile']['solar'] else 'no'}, "
                                   f"budget {rs(imp['profile']['budget'])}.", "⚡")]
        if stress:
            records.append(dict(FAKE))
            tr.append(step("Stress test", "Injected a fake 480-meal donation that no NGO confirmed.", "🧪"))
        return {"records": records, "window": list(kitchen.window), "profile": dict(imp["profile"]), "attempts": 0,
                "feedback": None, "trace": [step("LangGraph", "Collecting data from the ledger and Pillars 1-3.", "🕸️")] + tr}

    def verifier(s: ImpactState):
        ok, _, msg = lg.verify(imp["ledger"])
        accepted, rejected = im.verify_records(s["records"])
        tr = [step("Verifier", ("✅ " if ok else "❌ ") + msg, "🔐" if ok else "🛑"),
              step("Verifier", f"Rules accepted {len(accepted)} pickup(s); rejected {len(rejected)}.", "🧾")]
        for r in rejected:
            tr.append(step("Verifier", f"Rejected {r['post']}: {r['reason']}. It earns no points.", "🚫"))
        note = parse(VerifierNote, call_llm(
            f"You audit a food-rescue ledger. Rules already accepted {len(accepted)} and rejected {len(rejected)} records "
            f"({'; '.join(r['post'] + ': ' + r['reason'] for r in rejected) or 'none'}). Hash chain intact: {ok}. "
            'Add one short audit note. Reply JSON: {"note": "..."}'))
        if note:
            tr.append(step("Verifier", f"Llama ({llm_model()}) note: {note.note}", "🧠"))
        return {"ledger_ok": ok, "accepted": accepted, "rejected": rejected, "trace": tr}

    def scorer(s: ImpactState):
        tr, done = [], {e["ref"] for e in imp["ledger"] if e["ref"]}
        for r in s["accepted"]:
            if r["post"] in done:
                continue
            a = im.award(r)
            lg.add(imp["ledger"], ls.iso(t), a["kind"], a["text"], a["points"], a["co2_kg"], a["ref"])
            tr.append(step("Scorer", f"+{a['points']} Green Credit Points: {a['text']}.", "🪙"))
        sc = im.scores(imp["ledger"], s["accepted"], s["profile"], s["window"])
        tr.append(step("Scorer", f"{sc['points']:,} points ({sc['tier']['name']}), {sc['co2_kg']} kg CO2e avoided (mock), "
                                 f"{sc['meals']} verified meals, EV savings {rs(sc['ev_saving_rs'])}, donation share "
                                 f"{sc['donation_share_pct']}% vs {sc['donation_target_pct']}% target.", "🏅"))
        return {"scores": sc, "gaps": im.gaps(s["profile"], sc), "trace": tr}

    def advisor(s: ImpactState):
        n, prof = s["attempts"] + 1, s["profile"]
        cats = [c for c, _ in s["gaps"]]
        tr = [step("Green upgrade advisor", f"Attempt {n}: gaps: " + "; ".join(g for _, g in s["gaps"][:4])
                   + (f". Critic said: \"{s['feedback']}\"" if s.get("feedback") else "") + ".", "🌱")]
        cand = [im.best_offer(k) for k, p in im.PRODUCTS.items() if p["category"] in cats]
        lines = "\n".join(f"- {o['product']}: {im.PRODUCTS[o['product']]['name']} ({im.CATS[im.PRODUCTS[o['product']]['category']]['label']}), "
                          f"about {rs(o['price'])}, saves {rs(o['saving'])}/month, payback {o['payback']:g} months, cuts {o['co2']} kg CO2/month"
                          for o in cand)
        out = parse(AdvisorOutput, call_llm(
            f"Kitchen energy profile: {prof}. Budget {rs(prof['budget'])} including 18% GST. Gaps: {[g for _, g in s['gaps']]}.\n"
            f"Products (mock catalogue):\n{lines}\nRecommend the top 3 upgrades from DIFFERENT categories, total within budget, "
            f"each paying back within {im.R['max_payback_months']} months.\n"
            + (f"A critic rejected your last plan: {s['feedback']}\n" if s.get("feedback") else "")
            + 'Reply JSON: {"recommendations": [{"product": "key", "reason": "short"}]}'))
        if out and all(r.product in im.PRODUCTS for r in out.recommendations):
            recs, by = [r.model_dump() for r in out.recommendations], f"Llama ({llm_model()})"
        else:
            recs = [{"product": k, "reason": next(g for c, g in s["gaps"] if c == im.PRODUCTS[k]["category"])}
                    for k in im.recommend(prof, s["scores"])]
            by = "Rules (no LLM key or invalid JSON)"
        if stress and n == 1:  # deliberately over budget: the critic must catch it
            recs = [{"product": "solar_10kw", "reason": "stress test: deliberately over budget"}] + recs[:2]
            by = "Stress test"
        for r in recs:
            o = im.best_offer(r["product"])
            tr.append(step("Green upgrade advisor", f"{by}: {im.PRODUCTS[r['product']]['name']} · saves {rs(o['saving'])}/month · "
                                                    f"payback {o['payback']:g} months · cuts {o['co2']} kg CO2/month. {r['reason']}", "💡"))
        if not recs:
            tr.append(step("Green upgrade advisor", "No upgrade fits this budget and the 36-month payback rule.", "🤷"))
        return {"recs": recs, "attempts": n, "trace": tr}

    def procurement(s: ImpactState):
        tr = []
        table = {r["product"]: im.offers(r["product"]) for r in s["recs"]}
        for k, offs in table.items():
            tr.append(step("Compare tool", f"{im.PRODUCTS[k]['name']}: " + " | ".join(
                f"{o['vendor_name'].replace(' (mock)', '')} {rs(o['price'])}, {o['days']} d, ★{o['rating']}, +{o['points']} pts"
                for o in offs), "⚖️"))
        out = parse(ProcurementOutput, call_llm(
            "Pick the best-value vendor for each product (price, delivery days, rating, Green Points bonus). Offers:\n"
            + "\n".join(f"{k}: " + "; ".join(f"vendor {o['vendor']} price {o['price']} days {o['days']} rating {o['rating']} points {o['points']}"
                                            for o in offs) for k, offs in table.items())
            + '\nReply JSON: {"choices": [{"product": "key", "vendor": "vendor id", "reason": "short"}]}')) if table else None
        picked = {}
        if out:
            for c in out.choices:
                if c.product in table and c.vendor in {o["vendor"] for o in table[c.product]}:
                    picked[c.product] = (next(o for o in table[c.product] if o["vendor"] == c.vendor), c.reason, f"Llama ({llm_model()})")
        lines = []
        for k in table:
            o, why, by = picked.get(k) or (im.best_offer(k), "best value on price, rating, delivery and points", "Rules (best-value score)")
            lines.append({**o, "name": im.PRODUCTS[k]["name"], "category": im.PRODUCTS[k]["category"], "qty": 1, "reason": why})
            tr.append(step("Procurement", f"{by}: {o['vendor_name']} for {im.PRODUCTS[k]['name']} at {rs(o['price'])}. {why}.", "🛒"))
        balance = sum(e["points"] for e in imp["ledger"])
        money = im.price_lines(lines, balance)
        if lines:
            tr.append(step("Procurement", f"Draft order: subtotal {rs(money['subtotal'])}, Green Points discount -{rs(money['discount'])} "
                                          f"(max {im.R['max_points_discount_pct']}%), GST {rs(money['gst'])}, total {rs(money['total'])}.", "🧾"))
        return {"lines": lines, "money": money, "trace": tr}

    def critic(s: ImpactState):
        problems = im.critic(s["lines"], s["money"], s["profile"]["budget"])
        if not problems:
            return {"approved": True, "flagged": False, "feedback": None, "trace": [step("Critic", (
                f"Order passes every rule: within budget, payback ≤ {im.R['max_payback_months']} months, one per category, discount ≤ 10%."
                if s["lines"] else "Nothing to order. Only the report remains."), "✅")]}
        if s["attempts"] < MAX_ATTEMPTS:
            return {"approved": False, "feedback": problems[0],
                    "trace": [step("Critic", f"Rejected: {problems[0]}. Sending it back to the advisor.", "🔁")]}
        keys = im.recommend(s["profile"], s["scores"])
        lines = [{**im.best_offer(k), "name": im.PRODUCTS[k]["name"], "category": im.PRODUCTS[k]["category"], "qty": 1,
                  "reason": "rules fallback"} for k in keys]
        return {"approved": True, "flagged": True, "lines": lines, "money": im.price_lines(lines, s["scores"]["points"]),
                "trace": [step("Critic", f"Still wrong after {MAX_ATTEMPTS} tries: replaced with the rule-based plan and flagged.", "🛑")]}

    def after_critic(s: ImpactState):
        return "approval" if s["approved"] else "advisor"

    def approval(s: ImpactState):
        return {"trace": [step("Manager approval", "Graph paused. Press \"Approve order\" to place the demo order "
                                                  "and let the reporter write the ESG summary." if s["lines"] else
                                                  "Graph paused. Press \"Approve\" to let the reporter write the ESG summary.", "⏸️")]}

    def reporter(s: ImpactState):
        rep = write_report(s["scores"], s["lines"])
        return {"report": rep, "trace": [step("Reporter", f"{rep['by']} wrote the ESG summary and the 3-line city summary.", "📰")]}

    g = StateGraph(ImpactState)
    for name, fn in [("collector", collector), ("verifier", verifier), ("scorer", scorer), ("advisor", advisor),
                     ("procurement", procurement), ("critic", critic), ("approval", approval), ("reporter", reporter)]:
        g.add_node(name, fn)
    g.add_edge(START, "collector")
    g.add_edge("collector", "verifier")
    g.add_edge("verifier", "scorer")
    g.add_edge("scorer", "advisor")
    g.add_edge("advisor", "procurement")
    g.add_edge("procurement", "critic")
    g.add_conditional_edges("critic", after_critic, {"advisor": "advisor", "approval": "approval"})
    g.add_edge("approval", "reporter")
    g.add_edge("reporter", END)
    return g.compile(checkpointer=SAVER, interrupt_before=["reporter"])


def stream(graph, inp, config, final):
    for update in graph.stream(inp, config, stream_mode="updates"):
        for node, changes in update.items():
            if not isinstance(changes, dict):
                continue  # the interrupt marker
            for t in changes.get("trace", []):
                yield t
            final.update(changes)
    return final


def run(kitchen, stress=False):
    """Stream the graph until it pauses for manager approval. Returns (run id, final state)."""
    run_id = uuid.uuid4().hex[:8]
    graph = build(kitchen, stress)
    GRAPHS.clear()  # only the latest run can be approved
    GRAPHS[run_id] = graph
    final = yield from stream(graph, {}, {"configurable": {"thread_id": run_id}}, {})
    return run_id, final


def resume(run_id):
    """Continue a paused run after approval: the reporter node runs. Raises KeyError if the run is gone."""
    graph = GRAPHS[run_id]
    final = yield from stream(graph, None, {"configurable": {"thread_id": run_id}}, {})
    return final.get("report")
