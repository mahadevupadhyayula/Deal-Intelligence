"""LeadRepo: persistence for LeadProfile.

WHAT: upsert/get/list of lead profiles for the session's tenant.
WHERE: Used by the Lead Research agent (writes) and Wiki/Handoff agents (reads).
WHY: Every statement has an explicit `WHERE tenant_id = %s` in addition to RLS, so a
mis-provisioned role or policy still cannot leak rows (belt and braces).
"""
from psycopg.types.json import Jsonb

from agents.repos.session import TenantSession
from agents.schemas import LeadProfile


class LeadRepo:
    def __init__(self, session: TenantSession):
        self.s = session

    def upsert(self, lead: LeadProfile) -> None:
        self.s.require_own(lead)
        self.s.execute(
            "INSERT INTO leads (tenant_id, lead_id, data) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id, lead_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (self.s.tenant_id, lead.lead_id, Jsonb(lead.model_dump(mode="json"))),
        )

    def get(self, lead_id: str) -> LeadProfile | None:
        row = self.s.execute(
            "SELECT data FROM leads WHERE tenant_id = %s AND lead_id = %s",
            (self.s.tenant_id, lead_id),
        ).fetchone()
        return LeadProfile.model_validate(row[0]) if row else None

    def list(self) -> list[LeadProfile]:
        rows = self.s.execute(
            "SELECT data FROM leads WHERE tenant_id = %s ORDER BY lead_id", (self.s.tenant_id,)
        ).fetchall()
        return [LeadProfile.model_validate(r[0]) for r in rows]
