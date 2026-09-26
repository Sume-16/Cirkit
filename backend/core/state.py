"""
Kitchen state: the rolling 10-day queue, current stock, the active plan and impact totals,
plus Pillar 2's tagged batches, demo clock and routing plan (shared by every pillar).
The queue is a deque(maxlen=10): appending a new day automatically drops the oldest (FIFO).
"""
import json
from collections import deque
from datetime import date, timedelta

from . import batches
from . import impact as green
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
        self.reset_expiry()
        self.reset_rescue()
        self.green = green.seed()
        self.save()

    def reset_rescue(self):
        """Pillar 3: cooked-food posts and the records Pillar 4 will use."""
        self.rescue_posts, self.rescue_records, self.rescue_seq = [], [], 0

    def reset_expiry(self):
        """Pillar 2: tagged batches, demo clock, routing plan."""
        self.clock_h = 0
        batches.seed(self)
        self.expiry_plan = None
        self.expiry_impact = {"runs": 0}

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
            "approved_order": self.approved_order, "history": self.history, "impact": self.impact,
            "batches": self.batches, "batch_seq": self.batch_seq, "clock_h": self.clock_h,
            "expiry_plan": self.expiry_plan, "expiry_impact": self.expiry_impact,
            "rescue_posts": self.rescue_posts, "rescue_records": self.rescue_records, "rescue_seq": self.rescue_seq,
            "green": self.green}))

    def load(self):
        if not STATE_FILE.exists():
            return
        s = json.loads(STATE_FILE.read_text())
        self.reset_rescue()
        if "rescue_posts" in s:  # state saved before Pillar 3 existed has none
            self.rescue_posts, self.rescue_records, self.rescue_seq = s["rescue_posts"], s["rescue_records"], s["rescue_seq"]
        self.green = s.get("green") or green.seed()  # Pillar 4 (Revenue & Impact); older saves have none
        self.window = deque(s["window"], maxlen=WINDOW)
        self.stock, self.plan = s["stock"], s["plan"]
        self.approved_order, self.history, self.impact = s["approved_order"], s["history"], s["impact"]
        if "batches" not in s:  # state saved before Pillar 2 existed
            self.reset_expiry()
            return
        self.batches, self.batch_seq, self.clock_h = s["batches"], s["batch_seq"], s["clock_h"]
        self.expiry_plan, self.expiry_impact = s["expiry_plan"], s["expiry_impact"]


STATE = KitchenState.__new__(KitchenState)
if STATE_FILE.exists():
    STATE.load()
else:
    STATE.reset()
