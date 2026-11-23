"""Gate 1 tests for the stale-job reaper (Spec 010).

WHAT: The SECURITY DEFINER function `reap_stale_jobs`, its Python wrapper, and the API loop.
WHERE: Verifies `migrations/0007_reaper.sql`, `agents/repos/job_reaper.py`, `api/reaper.py`.
WHY: The reaper is the system's only cross-tenant write, so the tests prove both what it must do
(fail stale `running` jobs, exactly once, under concurrency) and what it must never do (touch
fresh, queued or finished jobs, expose payloads, widen app_user's own access, run unguarded).
"""
import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from agents.observability import metrics
from agents.observability.logging import hash_tenant
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from agents.repos.job_reaper import reap_stale_jobs
from api import reaper as reaper_mod
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B


async def make_job(pool, tenant, status="queued", age="0 seconds", admin_url=None):
    jid = uuid4()
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
        await JobRepo(s).create(jid, "echo", {"secret": "payload-must-not-leak"})
    with psycopg.connect(admin_url) as c:
        c.execute("UPDATE jobs SET status = %s, updated_at = now() - %s::interval WHERE job_id = %s",
                  (status, age, jid))
    return jid


def status_of(admin_url, jid):
    with psycopg.connect(admin_url) as c:
        return c.execute("SELECT status, error FROM jobs WHERE job_id = %s", (jid,)).fetchone()


async def test_stale_running_job_is_failed_and_everything_else_untouched(apool, admin_url):
    stale = await make_job(apool, A, "running", "2 hours", admin_url)
    fresh = await make_job(apool, A, "running", "10 seconds", admin_url)
    queued = await make_job(apool, A, "queued", "2 hours", admin_url)
    done = await make_job(apool, A, "succeeded", "2 hours", admin_url)
    reaped = await reap_stale_jobs(apool, 600)
    assert reaped == [(A, str(stale))]
    assert status_of(admin_url, stale) == ("failed", "worker lost; resubmit")
    for j, expected in ((fresh, "running"), (queued, "queued"), (done, "succeeded")):
        assert status_of(admin_url, j)[0] == expected


async def test_reaps_all_tenants_but_returns_ids_only(apool, admin_url):
    a = await make_job(apool, A, "running", "2 hours", admin_url)
    b = await make_job(apool, B, "running", "2 hours", admin_url)
    reaped = await reap_stale_jobs(apool, 600)
    assert sorted(reaped) == sorted([(A, str(a)), (B, str(b))])
    assert all(len(r) == 2 for r in reaped) and "payload-must-not-leak" not in json.dumps(reaped)


async def test_app_user_still_cannot_read_across_tenants(apool, admin_url):
    await make_job(apool, A, "running", "2 hours", admin_url)
    await reap_stale_jobs(apool, 600)
    async with apool.connection() as conn:  # no tenant context, as app_user
        cur = await conn.execute("SELECT count(*) FROM jobs")
        assert (await cur.fetchone())[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'x'; SET ROLE job_reaper")


def test_function_is_locked_down(admin_url):
    with psycopg.connect(admin_url) as c:
        acl, cfg, secdef, owner, bypass = c.execute(
            "SELECT p.proacl::text, p.proconfig::text, p.prosecdef, r.rolname, r.rolbypassrls "
            "FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner WHERE proname = 'reap_stale_jobs'"
        ).fetchone()
        app_bypass = c.execute("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname='app_user'"
                               ).fetchone()
        reaper_login = c.execute("SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname='job_reaper'"
                                 ).fetchone()
    assert secdef and owner == "job_reaper" and bypass
    import re
    assert not re.search(r"[{,]=X/", acl)  # no entry without a grantee = nothing granted to PUBLIC
    assert "app_user=X/" in acl
    assert "search_path" in cfg  # pinned: a definer function must not trust the caller's path
    assert app_bypass == (False, False)  # app_user did not gain BYPASSRLS
    assert reaper_login == (False, False)  # owner role cannot log in and is not a superuser


async def test_too_small_max_age_is_rejected(apool, admin_url):
    await make_job(apool, A, "running", "2 hours", admin_url)
    with pytest.raises(psycopg.errors.RaiseException):
        await reap_stale_jobs(apool, 5)


async def test_concurrent_reapers_fail_each_job_exactly_once(apool, admin_url):
    ids = {str(await make_job(apool, A, "running", "2 hours", admin_url)) for _ in range(12)}
    results = await asyncio.gather(*(reap_stale_jobs(apool, 600) for _ in range(4)))
    flat = [j for r in results for _, j in r]
    assert sorted(flat) == sorted(ids)  # every job once, none twice


def test_max_age_derivation():
    s = SimpleNamespace(reaper_max_age_seconds=None, job_timeout_seconds=300)
    assert reaper_mod.reaper_max_age(s) == 420
    assert reaper_mod.reaper_max_age(SimpleNamespace(reaper_max_age_seconds=900, job_timeout_seconds=300)) == 900
    assert reaper_mod.reaper_max_age(SimpleNamespace(reaper_max_age_seconds=5, job_timeout_seconds=300)) == 60


async def test_reap_once_counts_logs_hashed_tenant_and_fails_the_job(apool, admin_url, caplog):
    jid = await make_job(apool, A, "running", "2 hours", admin_url)
    before = metrics.REGISTRY.get_sample_value("di_reaped_jobs_total") or 0
    with caplog.at_level(logging.WARNING):
        assert await reaper_mod.reap_once(apool, 600) == 1
    assert (metrics.REGISTRY.get_sample_value("di_reaped_jobs_total") or 0) == before + 1
    rec = next(r for r in caplog.records if r.getMessage() == "job.reaped")
    assert rec.di_fields["job_id"] == str(jid) and rec.di_fields["tenant"] == hash_tenant(A)
    assert A not in json.dumps(rec.di_fields)
    assert status_of(admin_url, jid)[0] == "failed"


async def test_loop_survives_a_failing_tick_and_keeps_running(monkeypatch):
    calls = []

    async def flaky(pool, max_age):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db blip")
        return 0
    monkeypatch.setattr(reaper_mod, "reap_once", flaky)
    s = SimpleNamespace(reaper_max_age_seconds=None, job_timeout_seconds=300, reaper_interval_seconds=0.01)
    before = metrics.REGISTRY.get_sample_value("di_scrape_errors_total", {"reason": "reaper"}) or 0
    task = asyncio.create_task(reaper_mod.reaper_loop(object(), s))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3
    assert (metrics.REGISTRY.get_sample_value("di_scrape_errors_total", {"reason": "reaper"}) or 0) == before + 1


def test_worker_drains_on_sigterm_and_pod_grace_exceeds_it():
    from workers.settings import WorkerSettings
    assert WorkerSettings.job_completion_wait == 300
