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
Spec 008: a cancelled run (worker SIGTERM) is recorded as failed, not left `running`.
Known gap: a hard kill (SIGKILL/OOM) cannot be caught; a stale-`running` reaper is a follow-up.
Spec 009: job context (job_id, kind, hashed tenant) is bound for logging, and start/finish
events plus duration, queue-wait, outcome and provider-call metrics are recorded. Metric helpers
never raise, so instrumentation cannot change a job's outcome (including the cancel handling).
"""
import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from agents.config import get_settings
from agents.guardrails.limits import Limits, check_quota, utc_today
from agents.guardrails.redact import redact
from agents.guardrails.usage import MeteredLLM, MeteredSearch, UsageMeter, UsageRepo
from agents.job_kinds import PROVIDER_KINDS
from agents.jobs import JobContext
from agents.observability import metrics
from agents.observability.logging import bind_context, clear_context, hash_tenant, log_event
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from workers.handlers import JOB_HANDLERS

_log = logging.getLogger(__name__)

_MAX_ERROR = 2000


async def run_job(ctx: dict, tenant_id: str, job_id: str) -> None:
    bind_context(job_id=job_id, tenant=hash_tenant(tenant_id))
    try:
        await _run_job(ctx, tenant_id, job_id)
    finally:
        clear_context()  # direct calls (tests) share a task; ARQ jobs already get their own


async def _run_job(ctx: dict, tenant_id: str, job_id: str) -> None:
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
            metrics.observe_job(job.kind, "failed", 0.0)
            log_event(_log, "job.finished", kind=job.kind, status="failed",
                      error_type="QuotaExceeded", duration_ms=0)
            return  # no provider is called
        await JobRepo(s).mark_running(jid)
    # Committed here so "running" is visible while the handler works.
    bind_context(kind=job.kind)
    started = time.monotonic()
    wait = (datetime.now(UTC) - job.created_at).total_seconds()
    metrics.observe_queue_wait(job.kind, wait)
    log_event(_log, "job.started", queue_wait_ms=round(max(0.0, wait) * 1000))

    meter = UsageMeter()
    search, llm = ctx.get("search"), ctx.get("llm")
    job_ctx = JobContext(
        pool=pool,
        search=MeteredSearch(search, meter) if search is not None else None,
        llm=MeteredLLM(llm, meter) if llm is not None else None)
    cancelled, error_type = False, None
    try:
        async with asyncio.timeout(run_timeout):
            result = await JOB_HANDLERS[job.kind](job_ctx, tenant_id, job_id, job.payload)
        outcome = ("succeeded", result)
    except asyncio.CancelledError:
        # SIGTERM (deploy, scale-down) cancels the in-flight job. Found by the Spec 008 smoke test:
        # without this the row stayed `running` forever, and stranded `running` rows count toward
        # the tenant's active-job cap, eventually locking the tenant out. Record the outcome (and
        # any provider usage) below, then re-raise so the worker still shuts down.
        outcome, cancelled = ("failed", "worker stopped before the job finished; resubmit"), True
        error_type = "CancelledError"
    except TimeoutError:
        outcome = ("failed", f"timed out after {run_timeout:g}s")
        error_type = "TimeoutError"
    except Exception as exc:  # noqa: BLE001 - a handler may raise anything; record it
        outcome = ("failed", redact(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR])
        error_type = type(exc).__name__

    async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
        if outcome[0] == "succeeded":
            await JobRepo(s).mark_succeeded(jid, outcome[1])
        else:
            await JobRepo(s).mark_failed(jid, outcome[1])
        if job.kind in PROVIDER_KINDS and (meter.llm_calls or meter.searches):
            await UsageRepo(s).add(utc_today(), meter.llm_calls, meter.searches)
    duration = time.monotonic() - started
    status = "cancelled" if cancelled else outcome[0]
    metrics.observe_job(job.kind, status, duration)
    metrics.count_provider_calls("llm", meter.llm_calls, settings.price_per_llm_call)
    metrics.count_provider_calls("search", meter.searches, settings.price_per_search)
    log_event(_log, "job.finished", status=status, duration_ms=round(duration * 1000),
              llm_calls=meter.llm_calls, searches=meter.searches, error_type=error_type)
    if cancelled:
        raise asyncio.CancelledError
