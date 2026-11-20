"""HandoffRepo: persistence for HandoffBrief (one brief per deal).

WHAT: upsert/get of the latest handoff brief for a deal.
WHERE: Written by the Handoff agent on closed-won; read by the UI/implementation team.
WHY: One row per (tenant, deal) keeps "the current brief" simple; history can be added
later without changing callers.
"""
from psycopg.types.json import Jsonb

from agents.repos.session import TenantSession
from agents.schemas import HandoffBrief


class HandoffRepo:
    def __init__(self, session: TenantSession):
        self.s = session

    def upsert(self, b: HandoffBrief) -> None:
        self.s.require_own(b)
        self.s.execute(
            "INSERT INTO handoff_briefs (tenant_id, deal_id, data, generated_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, deal_id) "
            "DO UPDATE SET data = EXCLUDED.data, generated_at = EXCLUDED.generated_at",
            (self.s.tenant_id, b.deal_id, Jsonb(b.model_dump(mode="json")), b.generated_at),
        )

    def get(self, deal_id: str) -> HandoffBrief | None:
        row = self.s.execute(
            "SELECT data FROM handoff_briefs WHERE tenant_id = %s AND deal_id = %s",
            (self.s.tenant_id, deal_id),
        ).fetchone()
        return HandoffBrief.model_validate(row[0]) if row else None
