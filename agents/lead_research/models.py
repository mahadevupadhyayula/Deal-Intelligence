"""LLM-facing models for the Lead Research agent.

WHAT: The structured outputs the LLM must produce (`QueryPlan`, `ResearchFindings`) and the
job payload (`LeadResearchInput`).
WHERE: Used by `prompts.py`/`nodes.py`; `ResearchFindings` is what `validate.py` verifies.
WHY: These types deliberately contain NO tenant_id, segment keys, score or tier. The LLM only
reports evidence (url + verbatim quote) keyed by neutral criterion keys; code stamps tenants,
grades numbers and computes scores. `extra="forbid"` means an injected "score": 100 field makes
the output invalid instead of being silently kept.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QueryPlan(_Strict):
    queries: list[str] = Field(min_length=1, max_length=6)


class Evidence(_Strict):
    url: str = Field(min_length=1)
    quote: str = Field(min_length=10, max_length=400)


class MetricDraft(_Strict):
    value: float | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class JudgementDraft(_Strict):
    grade: Literal["ideal", "acceptable", "secondary", "none", "unknown"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)
    rationale: str = ""


class ResearchFindings(_Strict):
    # Informational only: never trusted for entity confirmation (see validate.py).
    official_domain: str | None = None
    industry: str | None = None
    signals: list[str] = Field(default_factory=list)
    metrics: dict[str, MetricDraft] = Field(default_factory=dict)
    judgements: dict[str, JudgementDraft] = Field(default_factory=dict)


class LeadResearchInput(_Strict):
    # ':' is the thread-id separator (agents/threads.py), so it is not allowed in ids.
    lead_id: str = Field(min_length=1, max_length=128, pattern=r"^[^:\s][^:]*$")
    company_name: str = Field(min_length=1, max_length=200)
    domain: str | None = Field(default=None, max_length=255)
