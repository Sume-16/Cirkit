"""Guardrail agent: sanity-checks the forecast before anyone cooks from it."""
from .common import step


def run(window, footfall):
    mean = sum(r["footfall"] for r in window) / len(window)
    lo, hi = 0.3 * mean, 1.8 * mean
    if footfall < lo or footfall > hi:
        fixed = min(max(footfall, lo), hi)
        yield step("Guardrail", f"Forecast {footfall:.0f} is outside the safe range ({lo:.0f}-{hi:.0f}). "
                                f"Limited to {fixed:.0f} and flagged for the manager.", "🛑")
        return fixed, True
    yield step("Guardrail", f"Forecast {footfall:.0f} passed checks (safe range {lo:.0f}-{hi:.0f}).", "✅")
    return footfall, False
