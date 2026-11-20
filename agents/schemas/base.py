"""TenantModel: the base class every domain model inherits.

WHAT: A Pydantic v2 base requiring a non-empty `tenant_id` and forbidding
unknown fields.
WHERE: Root of `agents/schemas/`; all persisted or queued data derives from it.
WHY: Multi-tenancy is a non-negotiable guardrail. Making `tenant_id` a required
field on the *base class* means a model without it cannot be constructed, so
tenant scoping fails loudly at the boundary instead of leaking silently later.
`extra="forbid"` catches typos and prompt-injected/LLM-hallucinated fields.
"""
from pydantic import BaseModel, ConfigDict, Field


class TenantModel(BaseModel):
    # Trade-off: strict mode off so JSON (str dates) validates; extras forbidden.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # min_length=1 rejects "" after whitespace stripping (strip runs first).
    tenant_id: str = Field(min_length=1)
