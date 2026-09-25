"""Shared helpers. Every agent is a generator: it yields trace steps for the
website and returns its result. The LangGraph nodes collect these steps."""
import json
import os

import requests


def step(agent, msg, icon="•"):
    return {"type": "step", "agent": agent, "msg": msg, "icon": icon}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def llm_model():
    return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def call_llm(prompt, system="You are a careful assistant for an Indian college canteen. Reply ONLY with JSON."):
    """Ask the open-source Llama model (hosted free on Groq) for a JSON answer.
    Returns None on any problem, so every agent can fall back to rules."""
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, timeout=25, json={
            "model": llm_model(), "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return json.loads(text.replace("```json", "").replace("```", "").strip())
    except Exception:
        return None


def collect(gen):
    """Run an agent generator: return (trace steps, result)."""
    steps = []
    while True:
        try:
            steps.append(next(gen))
        except StopIteration as done:
            return steps, done.value
