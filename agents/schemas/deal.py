"""DealState: the shared state object for one deal.

WHAT: Deal stage/amount plus the nested lead profile and open conflicts.
WHERE: Central state passed between agents; later the LangGraph state payload.
WHY: Nested models must share the parent's `tenant_id`; the validator below
stops a cross-tenant object from being attached to a deal (a data-leak vector).
"""
from datetime import UTC, datetime

from pydantic import Field, model_validator

from agents.schemas.base import TenantModel
from agents.schemas.conflict import ConflictItem
from agents.schemas.lead import LeadProfile


class DealState(TenantModel):
    deal_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    amount: float | None = Field(default=None, ge=0)
    lead: LeadProfile | None = None
    conflicts: list[ConflictItem] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _children_share_tenant(self) -> "DealState":
        children = ([self.lead] if self.lead else []) + self.conflicts
        for child in children:
            if child.tenant_id != self.tenant_id:
                raise ValueError(
                    f"tenant_id mismatch: {type(child).__name__} belongs to a different tenant"
                )
        return self
