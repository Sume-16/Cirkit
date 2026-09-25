# CirKit – Pillar 1: Demand planner
Team Yukthi (AX26-611) · Sumedha & Sravika · AGENT X 2026

Predicts how many customers a food institution will serve, how many portions of each
dish to cook, which raw ingredients are needed, and what to buy, so kitchens stop overproducing.

## How it works
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

## Run locally
    cd backend
    pip install -r requirements.txt
    copy .env.example .env      (Mac/Linux: cp .env.example .env) then paste your Groq key into .env
    uvicorn main:app --reload
Open docs/index.html in your browser. Test the API at http://localhost:8000/docs

## Deploy
1. Push this folder to GitHub (.env is ignored, so your key stays private).
2. Render → New → Blueprint → choose the repo (uses render.yaml). Add GROQ_API_KEY.
3. GitHub → Settings → Pages → branch main, folder /docs.
4. Paste the Render URL into DEFAULT_API in docs/index.html, or into the Backend box on the page.

## Data
backend/data/generate_data.py creates a synthetic month (1–30 Sep 2026) for a college canteen:
6 dishes, 20 raw ingredients with recipes, stock, pack sizes, prices and shelf life, and day labels.
Demand follows the structure of public food-demand datasets. Disclosed as synthetic.
