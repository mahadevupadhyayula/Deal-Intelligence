"""IcpEvaluation: the persisted result of evaluating one lead against a tenant's ICP.

WHAT: `StoredVerdict` (a criterion verdict plus its verified evidence quotes) and
`IcpEvaluation` (best segment, score, tier, per-segment scores, verdicts, flags).
WHERE: Built by the Lead Research agent's `persist` step (Spec 005), stored in
`lead_icp_evaluations`, returned by `GET /v1/leads/...`.
WHY: Storing the verdicts and quotes (not just the number) lets anyone audit a score and lets
us recompute it with `evaluate_segments`. `entity_confirmed` and `flags` carry the caveats a
human needs: "we could not confirm this is the right company", "disqualifier rests on one source".
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from agents.schemas.base import TenantModel
from agents.schemas.icp import CriterionVerdict, Rank, SegmentScore, Tier


class StoredVerdict(CriterionVerdict):
    evidence_quotes: list[str] = Field(default_factory=list)


class IcpEvaluation(TenantModel):
    evaluation_id: UUID
    lead_id: str = Field(min_length=1)
    job_id: UUID
    icp_versions: dict[str, int]
    best_segment_key: str
    best_rank: Rank
    score: float = Field(ge=0, le=100)
    tier: Tier
    segments: list[SegmentScore]
    verdicts: dict[str, list[StoredVerdict]]
    entity_confirmed: bool
    resolved_domain: str | None = None
    flags: list[str] = Field(default_factory=list)
    # Why a flag was raised (e.g. injection_suspected -> ["host: pattern"]), for reviewers.
    flags_detail: dict[str, list[str]] = Field(default_factory=dict)
    status: Literal["complete", "partial"]
    created_at: datetime
