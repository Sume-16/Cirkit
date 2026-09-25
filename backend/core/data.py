"""Loads the dataset files. All paths are relative to backend/data."""
import csv
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def load_json(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_truth():
    """Real demand per day (the 'future' the simulator reveals when a day closes)."""
    with open(DATA / "truth.csv", encoding="utf-8") as f:
        return {r["date"]: {k: int(v) if k != "date" else v for k, v in r.items()} for r in csv.DictReader(f)}


DISHES = load_json("dishes.json")
INGREDIENTS = load_json("ingredients.json")
CALENDAR = load_json("calendar.json")
TRUTH = load_truth()
