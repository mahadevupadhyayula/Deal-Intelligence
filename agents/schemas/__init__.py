"""Shared Pydantic v2 contracts exchanged between agents, API, and workers.

WHAT: Re-exports the core models.
WHERE: `agents/schemas/` is the lowest layer; everything else depends on it.
WHY: A single import surface (`from agents.schemas import DealState`) makes the
contracts easy to find and keeps the dependency direction one-way.
"""
from agents.schemas.base import TenantModel
from agents.schemas.conflict import ConflictItem
from agents.schemas.deal import DealState
from agents.schemas.evaluation import IcpEvaluation, StoredVerdict
from agents.schemas.handoff import HandoffBrief
from agents.schemas.icp import (
    CriterionVerdict,
    IcpResult,
    IcpSegment,
    IcpSegmentIn,
    SegmentScore,
)
from agents.schemas.job import JobRecord
from agents.schemas.lead import LeadProfile

__all__ = [
    "ConflictItem",
    "CriterionVerdict",
    "DealState",
    "HandoffBrief",
    "IcpEvaluation",
    "IcpResult",
    "IcpSegment",
    "IcpSegmentIn",
    "JobRecord",
    "LeadProfile",
    "SegmentScore",
    "StoredVerdict",
    "TenantModel",
]
