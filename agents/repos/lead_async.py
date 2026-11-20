"""AsyncLeadRepo: async persistence for LeadProfile.

WHAT: upsert/get of lead profiles on an AsyncTenantSession.
WHERE: Written by the Lead Research agent (persist), read by the leads API.
WHY: Same table and RLS contract as the sync LeadRepo (Spec 002), but non-blocking, because
the worker and API run on an event loop. A re-research overwrites the profile; the evaluation
history (lead_icp_evaluations) preserves the earlier evidence.
"""
from psycopg.types.json import Jsonb

from agents.repos.async_session import AsyncTenantSession
from agents.schemas import LeadProfile


class AsyncLeadRepo:
    def __init__(self, session: AsyncTenantSession):
        self.s = session

    async def upsert(self, lead: LeadProfile) -> None:
        self.s.require_own(lead)
        await self.s.execute(
            "INSERT INTO leads (tenant_id, lead_id, data) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id, lead_id) DO UPDATE SET data = EXCLUDED.data, "
            "updated_at = now()",
            (self.s.tenant_id, lead.lead_id, Jsonb(lead.model_dump(mode="json"))),
        )

    async def get(self, lead_id: str) -> LeadProfile | None:
        cur = await self.s.execute(
            "SELECT data FROM leads WHERE tenant_id = %s AND lead_id = %s",
            (self.s.tenant_id, lead_id))
        row = await cur.fetchone()
        return LeadProfile.model_validate(row[0]) if row else None
