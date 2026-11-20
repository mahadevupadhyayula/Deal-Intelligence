"""LeadProfile: output of the Lead Research agent.

WHAT: Firmographic data, buying signals, and cited sources for one lead.
WHERE: Produced by `agents/lead_research`; embedded in `DealState`; read by
the Wiki and Handoff agents.
WHY: A typed profile (with confidence and sources) lets downstream agents and
humans judge how much to trust LLM-gathered facts.
"""
from pydantic import AnyUrl, Field

from agents.schemas.base import TenantModel


class LeadProfile(TenantModel):
    lead_id: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    domain: str | None = None
    industry: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    signals: list[str] = Field(default_factory=list)
    sources: list[AnyUrl] = Field(default_factory=list)
    # 0..1 so scores from different tools are comparable.
    confidence: float = Field(ge=0.0, le=1.0)
