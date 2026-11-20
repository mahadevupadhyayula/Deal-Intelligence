"""AsyncEvaluationRepo: insert-only history of ICP evaluations plus ranked queries.

WHAT: create (idempotent per job), latest/history per lead, and a ranked "latest evaluation per
lead" list with tier/rank filters and keyset pagination.
WHERE: Written by the Lead Research agent; read by `api/routes/leads.py`.
WHY: Keyset pagination on (score DESC, lead_id ASC) stays stable while new evaluations arrive
(offset pagination would skip or repeat rows). The cursor is opaque to clients. Latest-per-lead
uses DISTINCT ON over the (tenant, lead, created_at) index.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from psycopg.types.json import Jsonb

from agents.repos.async_session import AsyncTenantSession
from agents.schemas import IcpEvaluation


def encode_cursor(score: float, lead_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([score, lead_id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        score, lead_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(score), str(lead_id)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


class AsyncEvaluationRepo:
    def __init__(self, session: AsyncTenantSession):
        self.s = session

    async def create(self, ev: IcpEvaluation) -> bool:
        """Insert; returns False if this job already persisted an evaluation (no-op)."""
        self.s.require_own(ev)
        cur = await self.s.execute(
            "INSERT INTO lead_icp_evaluations (tenant_id, evaluation_id, lead_id, job_id, "
            "best_segment_key, best_rank, score, tier, status, data, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, job_id) DO NOTHING",
            (self.s.tenant_id, ev.evaluation_id, ev.lead_id, ev.job_id, ev.best_segment_key,
             ev.best_rank, ev.score, ev.tier, ev.status, Jsonb(ev.model_dump(mode="json")),
             ev.created_at))
        return cur.rowcount == 1

    async def get_latest(self, lead_id: str) -> IcpEvaluation | None:
        cur = await self.s.execute(
            "SELECT data FROM lead_icp_evaluations WHERE tenant_id = %s AND lead_id = %s "
            "ORDER BY created_at DESC LIMIT 1", (self.s.tenant_id, lead_id))
        row = await cur.fetchone()
        return IcpEvaluation.model_validate(row[0]) if row else None

    async def list(self, lead_id: str) -> list[IcpEvaluation]:
        cur = await self.s.execute(
            "SELECT data FROM lead_icp_evaluations WHERE tenant_id = %s AND lead_id = %s "
            "ORDER BY created_at DESC", (self.s.tenant_id, lead_id))
        return [IcpEvaluation.model_validate(r[0]) for r in await cur.fetchall()]

    async def list_latest(
        self, tier: str | None = None, best_rank: str | None = None, limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Latest evaluation per lead, ranked by score desc then lead_id; returns (rows, next)."""
        where, params = ["TRUE"], [self.s.tenant_id]
        if tier:
            where.append("l.tier = %s")
            params.append(tier)
        if best_rank:
            where.append("l.best_rank = %s")
            params.append(best_rank)
        if cursor:
            score, lead_id = decode_cursor(cursor)
            where.append("(l.score < %s OR (l.score = %s AND l.lead_id > %s))")
            params += [score, score, lead_id]
        params.append(limit + 1)  # one extra row tells us whether another page exists
        cur = await self.s.execute(
            "SELECT l.lead_id, l.score, l.tier, l.best_segment_key, l.best_rank, l.status, "
            "l.created_at, ld.data->>'company_name' FROM ("
            "  SELECT DISTINCT ON (lead_id) lead_id, score, tier, best_segment_key, best_rank, "
            "         status, created_at FROM lead_icp_evaluations WHERE tenant_id = %s "
            "  ORDER BY lead_id, created_at DESC) l "
            "LEFT JOIN leads ld ON ld.tenant_id = %s AND ld.lead_id = l.lead_id "
            f"WHERE {' AND '.join(where)} ORDER BY l.score DESC, l.lead_id ASC LIMIT %s",
            (params[0], self.s.tenant_id, *params[1:]))
        rows = await cur.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [{"lead_id": r[0], "score": r[1], "tier": r[2], "best_segment_key": r[3],
                  "best_rank": r[4], "status": r[5], "created_at": r[6], "company_name": r[7]}
                 for r in rows]
        next_cursor = encode_cursor(items[-1]["score"], items[-1]["lead_id"]) if has_more else None
        return items, next_cursor
