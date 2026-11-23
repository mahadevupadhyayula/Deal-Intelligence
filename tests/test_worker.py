"""Gate 1 tests for the ARQ task `run_job` (called directly against the real test DB).

WHAT: Success, handler failure, wrong-tenant and duplicate-delivery behaviour.
WHERE: Verifies `workers/tasks.py`.
WHY: These are the cases that corrupt state if wrong: a forged tenant must not touch another
tenant's job, and a redelivered message must not re-run finished work.
"""
from uuid import uuid4

from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
from workers.handlers import JOB_HANDLERS
from workers.tasks import run_job


async def _create(pool, tenant, payload):
    jid = uuid4()
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
        await JobRepo(s).create(jid, "echo", payload)
    return jid


async def _get(pool, tenant, jid):
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
        return await JobRepo(s).get(jid)


async def test_success(apool):
    jid = await _create(apool, A, {"hi": 1})
    await run_job({"pool": apool}, A, str(jid))
    job = await _get(apool, A, jid)
    assert job.status == "succeeded" and job.result == {"echo": {"hi": 1}}


async def test_handler_failure_recorded(apool, monkeypatch):
    async def boom(*_):
        raise RuntimeError("kaput")
    monkeypatch.setitem(JOB_HANDLERS, "echo", boom)
    jid = await _create(apool, A, {})
    await run_job({"pool": apool}, A, str(jid))
    job = await _get(apool, A, jid)
    assert job.status == "failed" and job.error == "RuntimeError: kaput"


async def test_wrong_tenant_cannot_touch_job(apool):
    jid = await _create(apool, A, {})
    await run_job({"pool": apool}, B, str(jid))  # forged tenant on the message
    assert (await _get(apool, A, jid)).status == "queued"


async def test_duplicate_delivery_is_noop(apool, monkeypatch):
    calls = []

    async def counting(ctx, t, j, p):
        calls.append(j)
        return {}
    monkeypatch.setitem(JOB_HANDLERS, "echo", counting)
    jid = await _create(apool, A, {})
    await run_job({"pool": apool}, A, str(jid))
    await run_job({"pool": apool}, A, str(jid))
    assert len(calls) == 1 and (await _get(apool, A, jid)).status == "succeeded"


# ---- Spec 008: optional delay on the free echo job ------------------------------------------
async def test_echo_delay_disabled_by_default(monkeypatch):
    import time

    from agents.config import get_settings
    from agents.jobs import JobContext, echo

    monkeypatch.delenv("ECHO_MAX_DELAY_SECONDS", raising=False)
    get_settings.cache_clear()
    t0 = time.monotonic()
    out = await echo(JobContext(pool=None), "t", "j", {"delay_seconds": 5})
    assert out == {"echo": {"delay_seconds": 5}} and time.monotonic() - t0 < 1


async def test_echo_delay_is_clamped_to_the_cap(monkeypatch):
    import time

    from agents.config import get_settings
    from agents.jobs import JobContext, echo

    monkeypatch.setenv("ECHO_MAX_DELAY_SECONDS", "0.2")
    get_settings.cache_clear()
    try:
        t0 = time.monotonic()
        await echo(JobContext(pool=None), "t", "j", {"delay_seconds": 60})
        assert 0.15 <= time.monotonic() - t0 < 2  # asked 60s, capped at 0.2s
        t0 = time.monotonic()
        await echo(JobContext(pool=None), "t", "j", {"delay_seconds": True})  # bool is not a delay
        assert time.monotonic() - t0 < 0.1
    finally:
        monkeypatch.delenv("ECHO_MAX_DELAY_SECONDS")
        get_settings.cache_clear()


async def test_cancelled_job_is_recorded_failed_not_stranded_running(apool, monkeypatch):
    """Worker SIGTERM cancels the in-flight job: the row must end `failed`, and the
    cancellation must still propagate so the worker can exit (Spec 008 finding)."""
    import asyncio

    started = asyncio.Event()

    async def slow(ctx, t, j, p):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setitem(JOB_HANDLERS, "echo", slow)
    jid = await _create(apool, A, {})
    task = asyncio.create_task(run_job({"pool": apool}, A, str(jid)))
    await started.wait()
    assert (await _get(apool, A, jid)).status == "running"
    task.cancel()
    try:
        await task
        raised = False
    except asyncio.CancelledError:
        raised = True
    job = await _get(apool, A, jid)
    assert raised and job.status == "failed" and "worker stopped" in job.error
