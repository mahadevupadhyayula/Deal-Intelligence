"""Pydantic models for eval cases, runs, results and reports.

WHAT: `EvalCase` (one dataset entry), `CaseRun` (one execution), `Check`/`CaseResult`
(scorer verdicts), `SuiteReport` (everything a run produced, JSON-serialisable).
WHERE: Shared by loader, scorers, runner, report, regression, LangSmith sync and adapters.
WHY: Strict models (`extra="forbid"`) make a typo in a dataset file fail loudly at load time
instead of silently disabling a check. Target-specific data lives in free-form dicts so the core
stays agent-agnostic. `tenant_id` is deliberately NOT a case field: the runner owns tenancy.
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckSpec(_Strict):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class EvalCase(_Strict):
    id: str = Field(min_length=1)
    suite: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attack_class: str | None = None
    # Non-gating cases are reported (and counted in their own metric) but never fail the suite:
    # used for documented residual risks, so they are measured rather than hidden.
    gating: bool = True
    input: dict[str, Any]
    icp: str | None = None  # ICP file name in the dataset dir
    corpus: dict[str, Any] | None = None  # pinned search results (frozen mode)
    source_corpus: str | None = None  # name of a shared corpus file, resolved by the loader
    scripted: dict[str, Any] | None = None  # scripted-mode LLM plans/synths + config overrides
    control: str | None = None  # id of the baseline case this attack is compared with
    checks: list[CheckSpec] = Field(default_factory=list)
    pass_rate: float = Field(default=1.0, ge=0, le=1)  # share of repeats that must pass
    repeats: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _no_tenant(self):
        # The runner creates and owns the tenant; a case must never choose one.
        if "tenant_id" in self.input:
            raise ValueError("case input must not set tenant_id; the runner owns tenancy")
        return self


class CaseRun(_Strict):
    case_id: str
    repeat: int
    outcome: dict[str, Any] = Field(default_factory=dict)
    llm_calls: int = 0
    searches: int = 0
    duration_s: float = 0.0
    error: str | None = None


class Check(_Strict):
    name: str
    passed: bool
    detail: str = ""
    repeat: int | None = None


class CaseResult(_Strict):
    case_id: str
    attack_class: str | None = None
    gating: bool = True
    runs: list[CaseRun]
    checks: list[Check]
    passed_runs: int
    total_runs: int
    errors: int
    passed: bool


class SuiteReport(_Strict):
    suite: str
    mode: str
    model: str | None = None
    git_sha: str | None = None
    started_at: datetime
    results: list[CaseResult]
    skipped: list[str] = Field(default_factory=list)
    truncated: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)
    cost_estimate: dict[str, Any] = Field(default_factory=dict)
