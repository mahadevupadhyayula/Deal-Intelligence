"""Background loop that fails jobs stranded in `running` after a hard worker kill.

WHAT: `reaper_loop(pool, settings)` calls the reaper function every `REAPER_INTERVAL_SECONDS`;
`reaper_max_age` derives the staleness threshold.
WHERE: Started and cancelled by the API lifespan (`api/main.py`).
WHY run it in the API and not as a worker cron: with KEDA the worker Deployment scales to ZERO.
A tenant sitting at its active-job cap with stranded rows could not enqueue anything, so no
worker would ever start to reap them: a permanent lockout. The API is always running, its work
here is one small async query a minute (off the request path, so FastAPI is never blocked), and
several replicas are safe because the SQL function reaps each row exactly once. A failure in
one tick is logged and counted, never fatal.
"""
import asyncio
import logging

from agents.observability import metrics
from agents.observability.logging import hash_tenant, log_event
from agents.repos.job_reaper import reap_stale_jobs

_log = logging.getLogger(__name__)
_MIN_AGE = 60.0  # mirrors the SQL guard


def reaper_max_age(settings) -> float:
    """Configured age, else job timeout + 120 s so a live job can never look stale."""
    age = settings.reaper_max_age_seconds
    return max(_MIN_AGE, age if age is not None else settings.job_timeout_seconds + 120)


async def reap_once(pool, max_age: float) -> int:
    reaped = await reap_stale_jobs(pool, max_age)
    for tenant_id, job_id in reaped:
        metrics.count_reaped()
        # Tenants appear only as the keyed hash, like every other log line.
        log_event(_log, "job.reaped", level=logging.WARNING, job_id=job_id,
                  tenant=hash_tenant(tenant_id))
    return len(reaped)


async def reaper_loop(pool, settings) -> None:
    max_age = reaper_max_age(settings)
    if settings.reaper_max_age_seconds is not None and max_age < settings.job_timeout_seconds:
        _log.warning("REAPER_MAX_AGE_SECONDS is below JOB_TIMEOUT_SECONDS: live jobs may be reaped")
    while True:
        try:
            await reap_once(pool, max_age)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not stop the loop
            metrics.count_scrape_error("reaper")
            _log.exception("reaper tick failed")
        await asyncio.sleep(settings.reaper_interval_seconds)
