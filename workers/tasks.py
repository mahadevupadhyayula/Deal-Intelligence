"""ARQ task: run one job under its tenant.

WHAT: `run_job` loads the job, marks it running, dispatches to the registered handler, and
records success/failure.
WHERE: Invoked by the ARQ worker for messages the API enqueued; handlers come from
`agents/jobs.py` (agents plug in from Spec 005).
WHY: The message carries only ids, so the worker re-reads the job under an
AsyncTenantSession: RLS guarantees a forged/mismatched tenant_id finds nothing. Handler
failures are recorded, not retried (max_tries=1) until real agents define retry safety.
Error text is stored for the status API, so it is truncated and limited to the exception
type + message.
"""
from uuid import UUID

from agents.jobs import JOB_HANDLERS
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo

_MAX_ERROR = 2000


async def run_job(ctx: dict, tenant_id: str, job_id: str) -> None:
    pool, jid = ctx["pool"], UUID(job_id)

    async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
        job = await JobRepo(s).get(jid)
        if job is None or job.status != "queued":
            return  # unknown/foreign tenant, or duplicate delivery: do nothing
        await JobRepo(s).mark_running(jid)
    # Committed here so "running" is visible while the handler works.

    try:
        result = await JOB_HANDLERS[job.kind](tenant_id, job_id, job.payload)
        outcome = ("succeeded", result)
    except Exception as exc:  # noqa: BLE001 - a handler may raise anything; record it
        outcome = ("failed", f"{type(exc).__name__}: {exc}"[:_MAX_ERROR])

    async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
        if outcome[0] == "succeeded":
            await JobRepo(s).mark_succeeded(jid, outcome[1])
        else:
            await JobRepo(s).mark_failed(jid, outcome[1])
