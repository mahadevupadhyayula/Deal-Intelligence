"""HandoffBrief: sales-to-implementation summary for a won deal.

WHAT: Commitments, stakeholders, risks, and open questions for implementation.
WHERE: Produced by the Handoff agent; consumed by the implementation team/UI.
WHY: A structured brief (not free text) is validated, diffable, and renderable
in multiple surfaces.
"""
from datetime import UTC, datetime

from pydantic import Field

from agents.schemas.base import TenantModel


class HandoffBrief(TenantModel):
    deal_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    commitments: list[str] = Field(default_factory=list)
    stakeholders: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
