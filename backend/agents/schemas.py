"""Pydantic models that validate the LLM's JSON. If Llama returns anything that does not
fit these shapes, validation fails and the agent falls back to its rules."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

Action = Literal["use_first", "chef_special", "staff_meal", "donate"]


class RouteDecision(BaseModel):
    batch: str
    action: Action
    recipe: Optional[str] = Field(default=None, max_length=160)
    reason: str = Field(default="", max_length=240)


class RouterOutput(BaseModel):
    decisions: List[RouteDecision]


class MatchOutput(BaseModel):  # Pillar 3 matcher
    partner: str = Field(max_length=20)
    reason: str = Field(default="", max_length=240)


class OutreachOutput(BaseModel):  # Pillar 3 outreach
    message: str = Field(min_length=10, max_length=500)


class VerifierNote(BaseModel):  # Pillar 4 verifier (the LLM only comments; rules decide)
    note: str = Field(max_length=300)


class Recommendation(BaseModel):
    product: str = Field(max_length=40)
    reason: str = Field(default="", max_length=240)


class AdvisorOutput(BaseModel):  # Pillar 4 green upgrade advisor
    recommendations: List[Recommendation] = Field(min_length=1, max_length=3)


class Choice(BaseModel):
    product: str = Field(max_length=40)
    vendor: str = Field(max_length=40)
    reason: str = Field(default="", max_length=240)


class ProcurementOutput(BaseModel):  # Pillar 4 procurement agent
    choices: List[Choice] = Field(min_length=1, max_length=3)


class ReporterOutput(BaseModel):  # Pillar 4 reporter
    esg_summary: str = Field(min_length=20, max_length=700)
    city_summary: List[str] = Field(min_length=3, max_length=3)


def parse(model, data):
    """Validate LLM JSON against a model. Returns the model or None."""
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None
