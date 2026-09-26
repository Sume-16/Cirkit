# CirKit – Circular Kitchen Exchange
Team Yukthi (AX26-611) · Sumedha & Sravika · AGENT X 2026

"Your kitchen's waste is someone's raw material." Agentic AI for institutional kitchens (college canteens).

| Pillar | Status | What it does |
|---|---|---|
| 1 · Demand planner | Live | Predicts customers, portions per dish, raw ingredients and what to buy |
| 2 · Expiry & batch tracking | Live | Tags every delivery as a batch, counts down shelf life, FEFO, routes at-risk food |
| 3 · Redistribution network | Live | Cooked leftovers: safe food donated to NGOs, the rest recycled (animal feed, biogas, compost, used oil) |
| 4 · Revenue & Impact | Live | "Go green, and the savings come full circle." Green Credit Points on a SHA-256 ledger, GreenLoop Marketplace, ESG and government dashboards, how CirKit earns |

The website (docs/index.html) has a left sidebar with a **Dashboard** tab and one tab per pillar. The **Donate ⇄ Recycle** slider sits at the top right on every tab.
Add `?api=http://localhost:8010` to the page URL to point it at another backend, and `?mode=recycle` to open in Recycle mode.

## Pillar 1 – Demand planner
Planning runs as a **LangGraph** agent graph. The brain of the Forecaster and Critic agents is
**Llama 3.3 70B** (open-source, by Meta), called free through **Groq**. The maths agents are tools.

    observe -> forecaster (Llama) -> planner -> critic (rules + Llama)
                    ^                              |
                    +------ rejected: retry -------+   (max 3 attempts)
                                                   |
                                     approved -> inventory -> manager approves on the website

| Node / tool | File | What it does |
|---|---|---|
| Graph | agents/graph.py | LangGraph StateGraph: nodes, edges and the critic's retry loop |
| Queue tool | agents/queue_agent.py | Reads the 10-day window (deque, maxlen=10) and measures waste and sold-outs |
| Pattern tool | agents/pattern_agent.py | Footfall per day label, dish popularity, raises dishes that sold out |
| Learning tool | agents/learning_agent.py | Compares past forecasts with actuals and corrects bias |
| Forecaster agent | agents/graph.py | Llama decides tomorrow's customers from the evidence and explains why |
| Planner tools | agents/dish_planner.py, ingredient_agent.py | Portions per dish (5% buffer to Pillar 3), recipe × portions = ingredients |
| Critic agent | agents/graph.py | Safety rules + Llama review. Bad plan → feedback → back to the forecaster |
| Inventory agent | agents/inventory_agent.py | Perishables: buy only today's need. Bulk: reorder below 2 days' cover |
| LLM helper | agents/common.py | Calls Groq; returns None on failure so every agent falls back to rules |
| Orchestrator | agents/orchestrator.py | Runs the graph, approves orders, closes the day and rolls the queue |

## Pillar 2 – Expiry & batch tracking
Approved purchase orders become tagged batches (e.g. "Curd B2") with a sealed shelf life.
"Mark opened" switches a batch to its shorter opened shelf life. Cooking uses stock
**FEFO** (first expired, first out). A FEFO projection simulates the next 7 cooking days to find
food that will expire unused, even if it still looks fresh today (early warning), and shows it as
**value at risk (Rs)**.

    scan -> safety_gate (RULES ONLY) -> router (Llama) -> safety critic (rules)
                                            ^                    |
                                            +-- unsafe: retry ---+   (max 3, then rules override + flag)
                                                                 |
                                                   approved -> dispatch -> routing plan shown to the kitchen team

**Food safety is always rule-based.** The safety gate decides what is expired or too close to expiry
(discard only). Llama only sees batches the rules marked safe, with the actions they allow, and
suggests: use first / chef's special (with a recipe idea) / staff meal / donate (handoff to Pillar 3).
The safety critic re-checks every choice against the same rules. Shelf lives are FSSAI-guided
demo assumptions (not FSSAI-certified), all in one file: data/shelf_life.json.

| Node / tool | File | What it does |
|---|---|---|
| Graph | agents/expiry_graph.py | LangGraph StateGraph: scan, safety gate, router, safety critic, dispatch, retry loop |
| Batch ledger | core/batches.py | Tagged batches, demo clock, countdowns, FEFO use, FEFO projection, value at risk |
| Food-safety rules | core/food_safety.py | The only place that decides safety: status, allowed actions, checks (no LLM) |
| Shelf-life config | data/shelf_life.json | Sealed/opened hours, storage advice, safety thresholds, recipe hints |
| Scanner + safety gate | agents/batch_agent.py | Scans batches; splits into discard / FEFO-covered / needs routing |
| Router agent | agents/expiry_graph.py, router_agent.py | Llama picks an action per at-risk batch; rules fallback |
| LLM output schemas | agents/schemas.py | Pydantic models validate Llama's JSON; invalid → rules |
| Safety critic | agents/expiry_graph.py | Rejects unsafe or wasteful routing and sends feedback to the router |
| Orchestrator | agents/expiry_orchestrator.py | Runs the graph, mark opened, demo clock, Pillar 1 hooks |

How the pillars connect: Pillar 1's approved order → new batches. Pillar 1's close day → batches
used FEFO and the clock moves to the next morning.

### Pillar 2 API
| Method | Path | What it does |
|---|---|---|
| GET | /api/expiry/state | Batches with countdowns, status, FEFO rank, value at risk, plan, handoffs, impact |
| GET | /api/expiry/stream?stress=true | Runs the expiry agent graph, streams the live trace (SSE) |
| POST | /api/expiry/open?batch=Curd%20B1 | Mark a batch opened (shorter shelf life) |
| POST | /api/expiry/clock?hours=6 | Fast-forward the demo clock (1, 3, 6 or 12 h, max 18 h per day) |

## Pillar 3 – Redistribution network
Handles **cooked** food made by the kitchen team. A canteen, restaurant, cafeteria, caterer or food shelter
posts leftovers by hand and slides **Donate** (safe food → NGOs, shelters, community kitchens, free) or
**Recycle** (not fit for people → goshala / dairy farm, biogas, compost, used oil to biodiesel as guided by FSSAI RUCO; landfill last).
Everything happens in the Redistribution tab.

    safety (RULES ONLY) -> matcher (Llama + tools) -> router (route optimisation) -> critic (rules)
                                ^                                                     |
                                +---------------- rejected: retry --------------------+   (max 3, then rules + flag)
                                                                                      |
                                                            approved -> outreach (Llama, template fallback) -> dispatch

- **Safety check** (rules): consume-by = cooked-at + 6 h hot (60 °C+), 24 h chilled (5 °C or less), 2 h room temperature
  (FSSAI-guided demo assumptions). A Donate post that is past its window, has under 45 min left or looks spoiled is blocked,
  and the slider flips to Recycle with a friendly reason.
- **Matcher**: distance, capacity, veg-only and time tools; Llama picks the NGO (Pydantic-validated). Recycle uses the
  recovery ladder and each partner's accepted waste: clean veg → animal feed, else compost/biogas; meat, egg or moldy food never goes to cattle.
- **Router (route optimisation, estimated)**: road km = haversine × 1.3 at 20 km/h. Donate: ranks every NGO that arrives before
  consume-by with room, picks the nearest, keeps a backup (Route plan card, numbered pins, solid + dashed lines).
  Recycle: one nearest-neighbour pickup run through every pending partner, with total km. Real road routing (OSRM) is roadmap.
- **Critic**: rejects late arrival, over capacity, veg-only mismatch, unsafe food to people, wrong recycle partner, or a far NGO
  when one more than 1 km nearer also works. Stress test: the first match is the farthest NGO, so you can watch it loop back.
- **Follow-up agent**: if the NGO declines or doesn't accept within 10 min, the graph runs again without them and offers the
  food to the next best NGO (trace + "declined → reassigned" on the timeline).
- **Who's nearby** (changes with the slider): NGO cards with distance, ETA, call button, capacity, "arrives in time / too late",
  and Chat / Accept (as NGO) / Decline; recycle cards with what they accept, pickup days and "Send pickup request".
- **Chat board** (Donate): "Chatting as Kitchen / NGO" toggle, quick replies, short text box. "Is the food still available?"
  is answered instantly by a bot from live data (no LLM). **"Food collected ✅" marks the post Picked up and verifies it for Pillar 4.**
- **Records for Pillar 4**: kg, portions, destination type, partner, distance, vehicle, timestamps, `verified`, `verified_by`.
  Meals donated count only verified donations. No carbon maths yet.

All NGOs and recycle partners are mock, around Meerpet, Hyderabad, with demo phone numbers.

| Node / tool | File | What it does |
|---|---|---|
| Graph | agents/rescue_graph.py | LangGraph StateGraph: safety, matcher, router, critic, outreach, dispatch, retry loop |
| Safety and logistics rules | core/leftover_safety.py | The only place that decides safety; distance, ETA, fit, nearest-neighbour run, critic checks |
| Mock partners and rules | data/redistribution.json | 6 NGOs, 5 recycle partners, safe windows, speeds |
| Orchestrator | agents/rescue_orchestrator.py | Posts, follow-up agent, accept/decline, pickup requests, chat bot, verified records |
| Test | tests/test_pillar3.py | Full Pillar 3 flow plus Pillar 1, 2 and 4 checks (`python tests/test_pillar3.py`) |

| Method | Path | What it does |
|---|---|---|
| GET | /api/rescue/state | Posts (timeline, chat, route plan, fit per NGO), partners, pickup run, totals, records |
| POST | /api/rescue/check | Live safety preview + who can arrive in time (JSON body = the form) |
| POST | /api/rescue/posts | Create a post, saved at once (JSON body = the form; returns `blocked` if Donate food is unsafe) |
| GET | /api/rescue/stream?post=F1&stress=true | Runs the graph for a post, streams the trace (SSE) |
| POST | /api/rescue/accept?post=&ngo= · /decline?post=&ngo= (or &demo=true) | NGO accepts or declines (decline triggers the follow-up agent) |
| POST | /api/rescue/request?post=&partner= · /collected?post= | Recycle pickup request · mark collected |
| POST | /api/rescue/chat?post=&sender=kitchen\|ngo&text= | Chat; bot auto-answer; "Food collected ✅" verifies the pickup |

**Seeing "Not Found"?** The page is talking to an older backend build. `GET /` must list `"pillars": [1, 2, 3]`;
the site shows a warning if it doesn't. Stop the old server and run `uvicorn main:app --reload` inside `backend/`.

## Pillar 4 – Revenue & Impact
"Go green, and the savings come full circle." Verified impact from Pillars 1-3 earns **Green Credit Points**
(CirKit loyalty points, **not carbon credits**). Points buy green kitchen upgrades in the **GreenLoop Marketplace**
(up to 10% off), and the upgrades cut LPG, grid power, diesel and water. Every vendor, price, saving and CO2 figure is a
**mock estimate** in `data/marketplace.json`. Orders are demo orders: **no real payment is processed**.

    collector -> verifier (RULES + Llama note) -> scorer -> green upgrade advisor (Llama) -> procurement (Llama + compare tool)
                                                                 ^                                   |
                                                                 +---------- critic (rules) ---------+   (max 3 loops)
                                                                                    |
                                        approval (graph PAUSES; "Approve order" in the website) -> reporter (Llama) -> END

- **Verifier** (rules decide): SHA-256 chain intact; only donations the NGO confirmed with "Food collected ✅" count;
  spikes (over 150 portions or 3x the usual) are rejected.
- **Scorer**: 1 point per verified meal, 2 per recycled kg, +5 per EV pickup; Bronze / Silver (500) / Gold (1500);
  CO2 avoided, EV fuel savings, donation share vs a 10% target, landfill diversion vs a 30% goal.
- **Advisor + procurement**: top 3 upgrades from different categories within budget, then the best-value vendor
  (price, delivery, rating, points bonus) and a Green Points discount (1 point = Rs 1, max 10%).
- **Critic**: over budget, payback > 36 months, duplicate category or discount > 10% sends it back to the advisor.
- **Stress test**: injects a fake 480-meal donation (rejected, points unchanged) and an over-budget first
  recommendation (caught by the critic).
- **Orders**: one invoice per vendor (CK-2026-0001…, GST 18%, "Demo invoice - no real payment"), status
  placed → vendor confirmed → dispatched → delivered. Delivery adds the product's Green Points, writes a ledger entry and
  updates the energy profile. CirKit's 8% commission is paid by the vendor, not shown on the buyer's invoice.
- **How CirKit earns** (mock): vendor commission, kitchen subscriptions (Rs 4,999/month), CSR/BRSR report fees,
  government dashboard licence, recycler commission; self-sufficiency vs running cost. NGOs are always free.
- **Government · Pilot**: leaderboard, 30% landfill goal, certified NGO registry, charts, city summary and roadmap.

| Node / tool | File | What it does |
|---|---|---|
| Graph | agents/impact_graph.py | LangGraph with a checkpointer; pauses before the reporter until approval |
| Rules | core/impact.py | Verification, points, tiers, CO2, advisor fallback, vendor value, critic, invoice maths, revenue |
| Ledger | core/ledger.py | SHA-256 hash chain, verify, tamper test (on a copy) |
| Orchestrator | agents/impact_orchestrator.py | Run/approve, cart orders, order status, profile, dashboards |
| Mock config | data/marketplace.json | 17 vendors, 26 products (56 offers), profile, points, revenue, city data |
| Test | tests/test_pillar4.py | Full graph with both stress tests, approval, invoices, delivery, ledger, revenue |

| Method | Path | What it does |
|---|---|---|
| GET | /api/impact/state | Scores, ledger, catalogue, profile, orders, run, report, revenue, government |
| GET | /api/impact/stream?stress=true | Runs the graph until it pauses for approval (SSE) |
| POST | /api/impact/approve · /discard | Approve: place the orders, resume the graph (reporter) |
| POST | /api/impact/orders · /orders/{id}/advance | Place cart orders · simulate the next status |
| POST | /api/impact/profile | Update the kitchen energy profile |
| GET / POST | /api/impact/ledger/verify · /ledger/tamper-test | Verify the chain · tamper test on a copy |

## Demo assumptions
Pillar 3 redistribution assumptions. FSSAI-guided, NOT FSSAI-certified: safe-holding times for cooked food are
conservative demo assumptions (hot-held at 60 °C or above, chilled at 5 °C or below, room temperature about 2 hours).
Used cooking oil goes to a biodiesel collector as guided by FSSAI's RUCO initiative. ALL organisations are mock and
ALL phone numbers are demo numbers. Routes are estimated (haversine x 1.3 road factor, 20 km/h average city speed);
real road routing (OSRM) is on the roadmap.

## Run locally
    cd backend
    pip install -r requirements.txt
    copy .env.example .env      (Mac/Linux: cp .env.example .env) then paste your Groq key into .env
    uvicorn main:app --reload
Open docs/index.html in your browser. Test the API at http://localhost:8000/docs
Without a Groq key or internet, every agent uses its rules, so the demo still works.

## Deploy
1. Push this folder to GitHub (.env is ignored, so your key stays private).
2. Render → New → Blueprint → choose the repo (uses render.yaml). Add GROQ_API_KEY.
3. GitHub → Settings → Pages → branch main, folder /docs.
4. Paste the Render URL into DEFAULT_API in docs/index.html, or into the Backend box on the page.

## Data
backend/data/generate_data.py creates a synthetic month (1–30 Sep 2026) for a college canteen:
6 dishes, 20 raw ingredients with recipes, stock, pack sizes, prices and shelf life, and day labels.
Demand follows the structure of public food-demand datasets. Disclosed as synthetic.
backend/data/shelf_life.json holds Pillar 2's shelf-life and safety assumptions (FSSAI-guided).
