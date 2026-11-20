"""ConflictItem: one CRM-vs-evidence discrepancy.

WHAT: Records a field whose CRM value disagrees with evidence, its risk, and
its resolution status.
WHERE: Emitted by the CRM Hygiene agent; the `risk`/`status` fields drive the
HITL interrupt (high risk -> pending human approval).
WHY: Making risk and status explicit enums keeps the approval workflow
deterministic and auditable rather than buried in prompt text.
"""
from typing import Literal

from pydantic import Field

from agents.schemas.base import TenantModel


class ConflictItem(TenantModel):
    conflict_id: str = Field(min_length=1)
    deal_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    crm_value: str | None = None
    evidence_value: str | None = None
    evidence_source: str | None = None
    risk: Literal["low", "high"]
    status: Literal["pending", "approved", "rejected", "auto_applied"] = "pending"
