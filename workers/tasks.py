"""ARQ task: run one job under its tenant.

WHAT: `run_job` loads the job, marks it running, dispatches to the registered handler with a
`JobContext`, and records success/failure, including timeouts.
WHERE: Invoked by the ARQ worker for messages the API enqueued; handlers come from
`workers/handlers.py`.
WHY: The message carries only ids, so the worker re-reads the job under an
AsyncTenantSession: RLS guarantees a forged/mismatched tenant_id finds nothing. Handler
failures are recorded, not retried (max_tries=1) until agents define retry safety.
An INNER timeout (shorter than ARQ's) turns a stuck run into a recorded `failed: timed out`
instead of a job stranded in `running`. Error text is stored for the status API, so it is
truncated, redacted (provider errors can echo key fragments) and limited to the exception
type + message.
Spec 006: provider-using kinds re-check the daily quota before running (jobs may have queued
before the limit was hit), run on METERED adapters, and their usage is flushed after the
outcome, outside the timeout block, so timed-out and failed runs are still billed.
"""
import asyncio
from uuid import UUID

from agents.config import get_settings
from agents.guardrails.limits import Limits, check_quota, utc_today
from agents.guardrails.redact import redact
from agents.guardrails.usage import MeteredLLM, MeteredSearch, UsageMeter, UsageRepo
from agents.job_kinds import PROVIDER_KINDS
from agents.jobs import JobContext
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from workers.handlers import JOB_HANDLERS

_MAX_ERROR = 2000


async def run_job(ctx: dict, tenant_id: str, job_id: str) -> None:
    pool, jid = ctx["pool"], UUID(job_id)
    settings = get_settings()
    run_timeout = ctx.get("run_timeout", settings.research_run_timeout_seconds)
    limits = ctx.get("limits") or Limits.from_settings(settings)

    async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
        job = await JobRepo(s).get(jid)
        if job is None or job.status != "queued":
            return  # unknown/foreign tenant, or duplicate delivery: do nothing
        if job.kind in PROVIDER_KINDS and await check_quota(s, limits) is not None:
            await JobRepo(s).mark_failed(jid, "daily quota exceeded; resets at 00:00 UTC")
            return  # no provider is called
        await JobRepo(s).mark_running(jid)
    # Committed here so "running" is visible while the handler works.

    meter = UsageMeter()
    search, llm = ctx.get("search"), ctx.get("llm")
    job_ctx = JobContext(
        pool=pool,
        search=MeteredSearch(search, meter) if search is not None else None,
        llm=MeteredLLM(llm, meter) if llm is not None else None)
    try:
        async with asyncio.timeout(run_timeout):
            result = await JOB_HANDLERS[job.kind](job_ctx, tenant_id, job_id, job.payload)
        outcome = ("succeeded", result)
    except TimeoutError:
        outcome = ("failed", f"timed out after {run_timeout:g}s")
    except Exception as exc:  # noqa: BLE001 - a handler may raise anything; record it
        outcome = ("failed", redact(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR])

    async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
        if outcome[0] == "succeeded":
            await JobRepo(s).mark_succeeded(jid, outcome[1])
        else:
            await JobRepo(s).mark_failed(jid, outcome[1])
        if job.kind in PROVIDER_KINDS and (meter.llm_calls or meter.searches):
            await UsageRepo(s).add(utc_today(), meter.llm_calls, meter.searches)
