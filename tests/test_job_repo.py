"""Gate 1 tests for JobRepo and AsyncTenantSession (as non-superuser app_user).

WHAT: State-machine transitions and cross-tenant isolation for jobs.
WHERE: Verifies `agents/repos/job.py`, `async_session.py`, migration 0003.
WHY: The worker relies on guarded transitions for safety under duplicate delivery and on RLS
to refuse forged tenant ids.
"""
from uuid import uuid4

import pytest

from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import InvalidTransitionError, JobRepo
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B


async def test_happy_path_transitions(adb):
    jid = uuid4()
    async with AsyncTenantSession(adb, A) as s:
        repo = JobRepo(s)
        await repo.create(jid, "echo", {"x": 1})
        assert (await repo.get(jid)).status == "queued"
        await repo.mark_running(jid)
        await repo.mark_succeeded(jid, {"ok": True})
        job = await repo.get(jid)
    assert job.status == "succeeded" and job.result == {"ok": True} and job.tenant_id == A


async def test_invalid_transitions_raise(adb):
    jid = uuid4()
    async with AsyncTenantSession(adb, A) as s:
        repo = JobRepo(s)
        await repo.create(jid, "echo", {})
        with pytest.raises(InvalidTransitionError):
            await repo.mark_succeeded(jid, {})  # queued -> succeeded not allowed
        await repo.mark_running(jid)
        await repo.mark_succeeded(jid, {})
        with pytest.raises(InvalidTransitionError):
            await repo.mark_running(jid)  # succeeded -> running not allowed
        with pytest.raises(InvalidTransitionError):
            await repo.mark_failed(jid, "late")


async def test_failed_allowed_from_queued(adb):
    jid = uuid4()
    async with AsyncTenantSession(adb, A) as s:
        await JobRepo(s).create(jid, "echo", {})
        await JobRepo(s).mark_failed(jid, "queue unavailable")
        assert (await JobRepo(s).get(jid)).error == "queue unavailable"


async def test_cross_tenant_isolation(adb):
    jid = uuid4()
    async with AsyncTenantSession(adb, A) as s:
        await JobRepo(s).create(jid, "echo", {})
    async with AsyncTenantSession(adb, B) as s:
        assert await JobRepo(s).get(jid) is None
        with pytest.raises(InvalidTransitionError):
            await JobRepo(s).mark_running(jid)
    # Fail closed with no tenant set: raw SQL sees nothing.
    cur = await adb.execute("SELECT count(*) FROM jobs")
    assert (await cur.fetchone())[0] == 0


async def test_session_requires_tenant(adb):
    with pytest.raises(ValueError):
        AsyncTenantSession(adb, "")
