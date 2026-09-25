"""
Generates CirKit's synthetic dataset for a college canteen (September 2026).
Run once: python generate_data.py
Structure follows public food-demand datasets (date, footfall, orders per dish),
extended with day labels, recipes and ingredient stock. Disclose as synthetic.
"""
import csv, json, random
from datetime import date, timedelta

random.seed(42)
START, DAYS = date(2026, 9, 1), 30

DISHES = {
    "Veg biryani":   {"emoji": "🍛", "usual": 100, "bom": {"Basmati rice": .12, "Mixed vegetables": .08, "Onion": .05, "Tomato": .03, "Curd": .02, "Cooking oil": .012, "Ghee": .005, "Biryani masala": .004, "Ginger-garlic paste": .004, "Coriander & mint": .005, "Salt": .003, "Green chilli": .003}},
    "Sambar rice":   {"emoji": "🍲", "usual": 85,  "bom": {"Sona masoori rice": .12, "Toor dal": .04, "Mixed vegetables": .06, "Tomato": .02, "Onion": .02, "Sambar powder": .006, "Cooking oil": .006, "Salt": .003}},
    "Chapati & dal": {"emoji": "🫓", "usual": 85,  "bom": {"Wheat flour": .10, "Moong dal": .04, "Onion": .02, "Tomato": .02, "Cooking oil": .008, "Ghee": .003, "Salt": .003, "Green chilli": .002}},
    "Curd rice":     {"emoji": "🍚", "usual": 50,  "bom": {"Sona masoori rice": .10, "Curd": .12, "Green chilli": .002, "Salt": .002, "Cooking oil": .003}},
    "Aloo veg curry":{"emoji": "🥔", "usual": 50,  "bom": {"Potato": .10, "Mixed vegetables": .05, "Onion": .03, "Tomato": .03, "Cooking oil": .01, "Ginger-garlic paste": .003, "Salt": .002, "Coriander & mint": .003}},
    "Payasam":       {"emoji": "🥣", "usual": 42,  "bom": {"Milk": .12, "Sugar": .025, "Sona masoori rice": .015, "Ghee": .004}},
}

# unit, starting stock, vendor pack size, price per unit (Rs), shelf life (days)
INGREDIENTS = {
    "Basmati rice":        ["kg", 30, 25, 95, 180], "Sona masoori rice": ["kg", 45, 25, 52, 180],
    "Wheat flour":         ["kg", 10, 10, 38, 60],  "Toor dal":          ["kg", 6, 5, 140, 180],
    "Moong dal":           ["kg", 5, 5, 125, 180],  "Onion":             ["kg", 12, 10, 30, 10],
    "Tomato":              ["kg", 4, 5, 28, 4],     "Potato":            ["kg", 8, 10, 25, 14],
    "Mixed vegetables":    ["kg", 6, 5, 40, 3],     "Green chilli":      ["kg", 1, 1, 60, 5],
    "Ginger-garlic paste": ["kg", 2, 1, 160, 30],   "Coriander & mint":  ["kg", 0.5, 1, 80, 2],
    "Curd":                ["kg", 3, 5, 60, 3],     "Milk":              ["L", 0, 5, 56, 2],
    "Sugar":               ["kg", 6, 5, 44, 365],   "Cooking oil":       ["L", 8, 5, 150, 180],
    "Ghee":                ["kg", 1.5, 1, 600, 120],"Biryani masala":    ["kg", 0.3, 0.5, 700, 180],
    "Sambar powder":       ["kg", 0.4, 0.5, 400, 180], "Salt":            ["kg", 5, 5, 20, 365],
}

def vendor(name, shelf):
    if name in ("Milk", "Curd"): return "Vijaya Dairy"
    if shelf <= 14: return "Ramesh (Bowenpally mandi)"
    return "Sri Lakshmi Traders"

def label_for(d):
    special = {4: ("festival", "College fest: stalls and visiting students"),
               18: ("festival", "Cultural fest night"),
               11: ("normal", "Rain expected in the evening"),
               15: ("normal", "Guest lecture: about 50 visitors"),
               29: ("holiday", "Local holiday: most students away")}
    if d.day in special: return special[d.day]
    if 21 <= d.day <= 25: return ("exam", "Semester exams: students stay on campus")
    if d.weekday() >= 5: return ("weekend", "Weekend")
    return ("normal", "")

BASE = {"normal": 400, "weekend": 260, "festival": 560, "exam": 430, "holiday": 180}
SHARE = {"Veg biryani": .26, "Sambar rice": .20, "Chapati & dal": .20, "Curd rice": .12, "Aloo veg curry": .12, "Payasam": .10}
FEST_SHARE = {**SHARE, "Veg biryani": .32, "Payasam": .18}

calendar, rows = {}, []
for i in range(DAYS):
    d = START + timedelta(days=i)
    label, note = label_for(d)
    calendar[d.isoformat()] = {"label": label, "note": note}
    ff = BASE[label] * random.uniform(0.95, 1.05)
    if d.day == 11: ff *= 0.93
    if d.day == 15: ff += 50
    share = FEST_SHARE if label == "festival" else SHARE
    row = {"date": d.isoformat(), "footfall": round(ff)}
    for dish, s in share.items():
        row[dish] = round(ff * s * random.uniform(0.92, 1.08))
    rows.append(row)

with open("truth.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
json.dump(calendar, open("calendar.json", "w"), indent=2)
json.dump(DISHES, open("dishes.json", "w"), indent=2, ensure_ascii=False)
json.dump({k: {"unit": u, "stock": s, "pack": p, "price": pr, "shelf_days": sh, "vendor": vendor(k, sh)}
           for k, (u, s, p, pr, sh) in INGREDIENTS.items()}, open("ingredients.json", "w"), indent=2)
print(f"Generated {len(rows)} days, {len(DISHES)} dishes, {len(INGREDIENTS)} ingredients")
