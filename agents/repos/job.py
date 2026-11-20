"""JobRepo: async persistence and state machine for jobs.

WHAT: create/get plus guarded status transitions (queued -> running -> succeeded|failed).
WHERE: API creates jobs; the ARQ worker advances them.
WHY: Transitions are enforced in SQL (`WHERE status = <expected>`), so two workers racing
on the same job cannot both win, and a finished job can never be re-run by accident.
`failed` is also allowed from `queued` for the "Redis was down at enqueue" case.
"""
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from agents.repos.async_session import AsyncTenantSession
from agents.schemas import JobRecord

_COLS = "tenant_id, job_id, kind, status, payload, result, error, created_at, updated_at"


class InvalidTransitionError(RuntimeError):
    """Job missing (for this tenant) or not in a state that allows the transition."""


class JobRepo:
    def __init__(self, session: AsyncTenantSession):
        self.s = session

    async def create(self, job_id: UUID, kind: str, payload: dict[str, Any]) -> None:
        await self.s.execute(
            "INSERT INTO jobs (tenant_id, job_id, kind, status, payload) "
            "VALUES (%s, %s, %s, 'queued', %s)",
            (self.s.tenant_id, job_id, kind, Jsonb(payload)),
        )

    async def get(self, job_id: UUID) -> JobRecord | None:
        cur = await self.s.execute(
            f"SELECT {_COLS} FROM jobs WHERE tenant_id = %s AND job_id = %s",
            (self.s.tenant_id, job_id),
        )
        row = await cur.fetchone()
        return JobRecord(**dict(zip(_COLS.split(", "), row, strict=True))) if row else None

    async def _transition(self, job_id: UUID, to: str, allowed_from: tuple[str, ...],
                          result: dict | None = None, error: str | None = None) -> None:
        cur = await self.s.execute(
            "UPDATE jobs SET status = %s, result = %s, error = %s, updated_at = now() "
            "WHERE tenant_id = %s AND job_id = %s AND status = ANY(%s)",
            (to, Jsonb(result) if result is not None else None, error,
             self.s.tenant_id, job_id, list(allowed_from)),
        )
        if cur.rowcount != 1:
            raise InvalidTransitionError(f"cannot move job {job_id} to {to}")

    async def mark_running(self, job_id: UUID) -> None:
        await self._transition(job_id, "running", ("queued",))

    async def mark_succeeded(self, job_id: UUID, result: dict[str, Any]) -> None:
        await self._transition(job_id, "succeeded", ("running",), result=result)

    async def mark_failed(self, job_id: UUID, error: str) -> None:
        await self._transition(job_id, "failed", ("queued", "running"), error=error)
