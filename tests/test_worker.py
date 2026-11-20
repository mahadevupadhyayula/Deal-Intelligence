"""Gate 1 tests for the ARQ task `run_job` (called directly against the real test DB).

WHAT: Success, handler failure, wrong-tenant and duplicate-delivery behaviour.
WHERE: Verifies `workers/tasks.py`.
WHY: These are the cases that corrupt state if wrong: a forged tenant must not touch another
tenant's job, and a redelivered message must not re-run finished work.
"""
from uuid import uuid4

from agents.jobs import JOB_HANDLERS
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
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

    async def counting(t, j, p):
        calls.append(j)
        return {}
    monkeypatch.setitem(JOB_HANDLERS, "echo", counting)
    jid = await _create(apool, A, {})
    await run_job({"pool": apool}, A, str(jid))
    await run_job({"pool": apool}, A, str(jid))
    assert len(calls) == 1 and (await _get(apool, A, jid)).status == "succeeded"
