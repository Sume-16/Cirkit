"""
Context agent: reads the calendar note for the target day (rain, visitors...)
and proposes a bounded adjustment. The LLM advises; guardrails cap it.
Day labels are already handled by the Pattern agent, so this only covers extra context.
"""
import re

from .common import call_llm, clamp, step


def rules(note, footfall):
    text = note.lower()
    factor, why = 1.0, []
    if "rain" in text:
        factor *= 0.93
        why.append("rain usually keeps some customers away")
    m = re.search(r"(\d+)\s*(visitors|guests)", text)
    if m:
        factor *= 1 + int(m.group(1)) / footfall
        why.append(f"{m.group(1)} extra visitors")
    return {"factor": factor, "reason": "; ".join(why) or "nothing unusual beyond the day label"}


def run(day, label, note, footfall):
    yield step("Context", f"Calendar for {day}: label '{label}'" + (f", note: \"{note}\"." if note else ", no notes."), "📅")
    out = call_llm(
        "You help an Indian college canteen plan food. The day-type pattern is ALREADY in the baseline "
        f"of {footfall:.0f} customers, so do not adjust for the label '{label}' again. "
        f"Calendar note: \"{note or 'none'}\". Reply ONLY JSON: "
        '{"factor": number (customer multiplier, 1.0 if no effect), "reason": "short sentence"}'
    )
    source = "LLM"
    if not out or "factor" not in out:
        out, source = rules(note, footfall), "Rules"
    raw = float(out["factor"])
    factor = clamp(raw, 0.7, 1.3)
    capped = " (capped by guardrail)" if factor != raw else ""
    yield step("Context", f"{source}: x{factor:.2f}{capped}. {out['reason'].rstrip('.')}.", "🧠")
    return {"factor": factor, "reason": out["reason"], "source": source}
