"""Job submission and status endpoints.

WHAT: POST /v1/jobs (record + enqueue, 202) and GET /v1/jobs/{id}.
WHERE: The only way clients start agent work; the worker (workers/tasks.py) executes it.
WHY: The handler does O(1) work: insert a row, push a tiny message, return. Nothing slow
runs here, so latency is independent of how long the agent takes. The ARQ message holds
only (tenant_id, job_id); the payload stays in Postgres so Redis never stores tenant data.
Admission (Spec 006) runs first: a per-tenant rate limit (Redis), then, under a per-tenant
advisory lock, an active-job cap and the daily provider quota (Postgres), so concurrent
requests cannot jointly exceed a limit. Rejections are 429 with Retry-After.
Trade-off: DB insert and enqueue are not atomic. If Redis fails after the insert we mark
the job failed and return 503 (simple, honest); an outbox is a later upgrade.
"""
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from redis.exceptions import RedisError

from agents.guardrails.limits import RateLimiter, Rejection, check_admission, lock_tenant
from agents.job_kinds import JOB_KINDS, PROVIDER_KINDS
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from agents.schemas.job import JobStatus
from api.auth import current_tenant

router = APIRouter(prefix="/v1")


class JobCreate(BaseModel):
    # extra=forbid: a client-supplied tenant_id (or any stray field) is rejected, not ignored.
    model_config = ConfigDict(extra="forbid")
    kind: str
    payload: dict[str, Any] = {}

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in JOB_KINDS:
            raise ValueError(f"unknown job kind: {v}")
        return v


class JobAccepted(BaseModel):
    job_id: UUID
    status: JobStatus


class JobOut(BaseModel):
    job_id: UUID
    kind: str
    status: JobStatus
    result: dict[str, Any] | None
    error: str | None


def _too_many(r: Rejection) -> HTTPException:
    # Names the limit hit and when to retry; reveals nothing about other tenants.
    return HTTPException(429, {"error": r.code, "message": r.message},
                         headers={"Retry-After": str(r.retry_after)})


@router.post("/jobs", status_code=202, response_model=JobAccepted)
async def create_job(request: Request, tenant_id: str = Depends(current_tenant)):
    # Raw body so the size cap (413) applies before parsing; we validate by hand (422).
    raw = await request.body()
    if len(raw) > request.app.state.max_payload_bytes:
        raise HTTPException(413, "payload too large")
    try:
        body = JobCreate.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc

    limits = request.app.state.limits
    try:
        rejected = await RateLimiter(request.app.state.arq, limits.rate_per_minute).check(tenant_id)
    except (RedisError, OSError) as exc:  # fail closed: enqueueing needs Redis anyway
        raise HTTPException(503, "rate limiter unavailable") from exc
    if rejected:
        raise _too_many(rejected)

    pool, job_id = request.app.state.pool, uuid4()
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
        await lock_tenant(s)  # serialise count-then-insert for this tenant
        rejected = await check_admission(s, limits, body.kind in PROVIDER_KINDS)
        if rejected is None:
            await JobRepo(s).create(job_id, body.kind, body.payload)
    if rejected:
        raise _too_many(rejected)
    # Row is committed before enqueue, so the worker can never see a message without a row.

    try:
        await request.app.state.arq.enqueue_job("run_job", tenant_id, str(job_id),
                                                _job_id=str(job_id))
    except (RedisError, OSError) as exc:
        async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
            await JobRepo(s).mark_failed(job_id, "queue unavailable")
        raise HTTPException(503, "queue unavailable") from exc
    return JobAccepted(job_id=job_id, status="queued")


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: UUID, request: Request, tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        job = await JobRepo(s).get(job_id)
    if job is None:  # also covers "belongs to another tenant": 404, never 403
        raise HTTPException(404, "job not found")
    return JobOut(**job.model_dump(include={"job_id", "kind", "status", "result", "error"}))
