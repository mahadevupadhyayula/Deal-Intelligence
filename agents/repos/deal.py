"""DealRepo and ConflictRepo: persistence for DealState and ConflictItem.

WHAT: Stores a DealState as one `deals` row (minus conflicts) plus N `conflicts` rows,
saved atomically, and reassembles it on read.
WHERE: Used by the CRM Hygiene agent (conflicts, HITL status changes) and the API.
WHY: Conflicts are their own rows so HITL can query/update "pending" items without
rewriting the whole deal. `save` upserts only the conflicts it is given (never deletes
others) so a partial in-memory view can't erase the audit trail.
"""
from psycopg.types.json import Jsonb

from agents.repos.session import TenantSession
from agents.schemas import ConflictItem, DealState


class ConflictRepo:
    def __init__(self, session: TenantSession):
        self.s = session

    def upsert(self, c: ConflictItem) -> None:
        self.s.require_own(c)
        self.s.execute(
            "INSERT INTO conflicts (tenant_id, conflict_id, deal_id, risk, status, data) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, conflict_id) DO UPDATE SET deal_id = EXCLUDED.deal_id, "
            "risk = EXCLUDED.risk, status = EXCLUDED.status, data = EXCLUDED.data, updated_at = now()",
            (self.s.tenant_id, c.conflict_id, c.deal_id, c.risk, c.status,
             Jsonb(c.model_dump(mode="json"))),
        )

    def get(self, conflict_id: str) -> ConflictItem | None:
        row = self.s.execute(
            "SELECT data FROM conflicts WHERE tenant_id = %s AND conflict_id = %s",
            (self.s.tenant_id, conflict_id),
        ).fetchone()
        return ConflictItem.model_validate(row[0]) if row else None

    def list(self, deal_id: str | None = None, status: str | None = None) -> list[ConflictItem]:
        q = "SELECT data FROM conflicts WHERE tenant_id = %s"
        params: list = [self.s.tenant_id]
        if deal_id is not None:
            q += " AND deal_id = %s"
            params.append(deal_id)
        if status is not None:
            q += " AND status = %s"
            params.append(status)
        rows = self.s.execute(q + " ORDER BY conflict_id", tuple(params)).fetchall()
        return [ConflictItem.model_validate(r[0]) for r in rows]


class DealRepo:
    def __init__(self, session: TenantSession):
        self.s = session
        self._conflicts = ConflictRepo(session)

    def save(self, deal: DealState) -> None:
        self.s.require_own(deal)
        data = deal.model_dump(mode="json", exclude={"conflicts"})
        # Deal row first: conflicts have a composite FK to it. Same transaction => atomic.
        self.s.execute(
            "INSERT INTO deals (tenant_id, deal_id, stage, data) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, deal_id) DO UPDATE SET stage = EXCLUDED.stage, "
            "data = EXCLUDED.data, updated_at = now()",
            (self.s.tenant_id, deal.deal_id, deal.stage, Jsonb(data)),
        )
        for c in deal.conflicts:
            if c.deal_id != deal.deal_id:
                raise ValueError("conflict.deal_id does not match deal.deal_id")
            self._conflicts.upsert(c)

    def get(self, deal_id: str) -> DealState | None:
        row = self.s.execute(
            "SELECT data FROM deals WHERE tenant_id = %s AND deal_id = %s",
            (self.s.tenant_id, deal_id),
        ).fetchone()
        return self._assemble(row[0]) if row else None

    def list(self, stage: str | None = None) -> list[DealState]:
        q = "SELECT data FROM deals WHERE tenant_id = %s"
        params: list = [self.s.tenant_id]
        if stage is not None:
            q += " AND stage = %s"
            params.append(stage)
        rows = self.s.execute(q + " ORDER BY deal_id", tuple(params)).fetchall()
        return [self._assemble(r[0]) for r in rows]

    def _assemble(self, data: dict) -> DealState:
        # N+1 on list() is acceptable at this scale; revisit with a join when lists grow.
        conflicts = self._conflicts.list(deal_id=data["deal_id"])
        return DealState.model_validate({**data, "conflicts": [c.model_dump() for c in conflicts]})
