"""
Kitchen state: the rolling 10-day queue, current stock, the active plan and impact totals.
The queue is a deque(maxlen=10): appending a new day automatically drops the oldest (FIFO).
"""
import json
from collections import deque
from datetime import date, timedelta

from .data import DATA, DISHES, INGREDIENTS, TRUTH
from .simulator import usual_habits

WINDOW = 10
STATE_FILE = DATA / "state.json"
FIRST_DAY = min(TRUTH)


class KitchenState:
    def __init__(self):
        self.reset()

    def reset(self):
        start = date.fromisoformat(FIRST_DAY)
        seed = [usual_habits((start + timedelta(days=i)).isoformat()) for i in range(WINDOW)]
        self.window = deque(seed, maxlen=WINDOW)
        self.stock = {k: v["stock"] for k, v in INGREDIENTS.items()}
        self.plan = None
        self.approved_order = False
        self.history = []          # forecast vs actual, for learning and accuracy
        self.impact = {"days": 0, "portions_saved": 0, "stockouts_avoided": 0, "money_saved": 0}
        self.save()

    @property
    def next_date(self):
        return (date.fromisoformat(self.window[-1]["date"]) + timedelta(days=1)).isoformat()

    def has_future(self):
        return self.next_date in TRUTH

    def roll(self, record):
        """FIFO: enqueue the newest day, the oldest drops out automatically."""
        dropped = self.window[0]["date"] if len(self.window) == WINDOW else None
        self.window.append(record)
        return dropped

    def save(self):
        STATE_FILE.write_text(json.dumps({
            "window": list(self.window), "stock": self.stock, "plan": self.plan,
            "approved_order": self.approved_order, "history": self.history, "impact": self.impact}))

    def load(self):
        if not STATE_FILE.exists():
            return
        s = json.loads(STATE_FILE.read_text())
        self.window = deque(s["window"], maxlen=WINDOW)
        self.stock, self.plan = s["stock"], s["plan"]
        self.approved_order, self.history, self.impact = s["approved_order"], s["history"], s["impact"]


STATE = KitchenState.__new__(KitchenState)
if STATE_FILE.exists():
    STATE.load()
else:
    STATE.reset()
