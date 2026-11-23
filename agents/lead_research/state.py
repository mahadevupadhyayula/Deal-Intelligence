"""Graph state for one research run.

WHAT: `ResearchState`, the TypedDict passed between nodes.
WHERE: Used by `graph.py` and `nodes.py`.
WHY: Explicit, typed state makes data flow visible: what each node reads and writes. `trace`
uses an append reducer so tests can assert the exact node sequence. Counters (searches, LLM
calls, passes) live here, per run, so budget enforcement is race-free across concurrent jobs.
"""
import operator
from typing import Annotated, Any, TypedDict


class ResearchState(TypedDict, total=False):
    # inputs
    tenant_id: str
    lead_id: str
    job_id: str
    company_name: str
    domain: str | None
    # ICP
    segments: list
    plan: Any
    # research progress
    passes: int
    searches_used: int
    llm_calls_used: int
    partial: bool
    flags: list[str]
    flags_detail: dict
    canary: Any  # CanaryGuard for the current synthesize call
    queries: list[str]
    used_queries: list[str]
    sources: list[dict]
    resolved_domain: str | None
    findings: Any
    pending_keys: set
    pending_metrics: set
    judgements: dict
    metrics: dict
    industry: str | None
    signals: list[str]
    # evaluation
    verdicts: dict
    entity_confirmed: bool
    result: Any
    eval_flags: list[str]
    can_improve: bool
    # outputs
    profile: Any
    evaluation: Any
    trace: Annotated[list[str], operator.add]
