"""Learning agent: compares past forecasts with what really happened and corrects bias."""
from .common import clamp, step


def run(history):
    recent = history[-5:]
    if not recent:
        yield step("Learning", "No closed days yet. After each day closes I compare forecast vs actual and self-correct.", "📚")
        return 1.0
    ratio = sum(h["actual"] / h["forecast"] for h in recent) / len(recent)
    bias = clamp(ratio, 0.9, 1.1)
    err = sum(abs(h["actual"] - h["forecast"]) / h["actual"] for h in recent) / len(recent) * 100
    yield step("Learning", f"Last {len(recent)} forecasts were off by {err:.1f}% on average. "
                           f"Correction x{bias:.2f}.", "📚")
    return bias
