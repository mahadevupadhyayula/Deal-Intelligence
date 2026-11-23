"""Gate 1 tests for admission control, quotas, usage persistence and worker metering (Spec 006).

WHAT: API 429 behaviour (rate, active-job cap, daily quota), concurrency safety of the cap,
tenant isolation of every limit, `UsageRepo` atomicity + RLS, and `run_job` quota re-check,
usage flushing (success, failure, timeout) and error redaction.
WHERE: Verifies `api/routes/jobs.py`, `agents/guardrails/{limits,usage}.py`, `workers/tasks.py`.
WHY: Limits protect shared budget and fairness; a bug here either lets one tenant starve the
rest or bills nothing for real spend, so each rule is tested for both tenants and under load.
"""
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
import pytest

from agents.guardrails.limits import Limits
from agents.guardrails.redact import register_secret
from agents.guardrails.usage import UsageRepo
from agents.lead_research.tools import FixtureSearch, SearchResult
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from api.main import create_app
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
from tests.test_api import FakeArq, auth
from workers.handlers import JOB_HANDLERS
from workers.tasks import run_job

TODAY = datetime.now(UTC).date()


def make_client(apool, arq, **limit_kw):
    app = create_app(pool=apool, arq=arq, limits=Limits(**{"rate_per_minute": 1000, **limit_kw}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def set_usage(admin_url, tenant, llm=0, searches=0):
    with psycopg.connect(admin_url) as c:
        c.execute("INSERT INTO tenant_usage (tenant_id, day, llm_calls, searches) VALUES (%s,%s,%s,%s) "
                  "ON CONFLICT (tenant_id, day) DO UPDATE SET llm_calls=EXCLUDED.llm_calls, searches=EXCLUDED.searches",
                  (tenant, TODAY, llm, searches))


async def post(c, key, kind="echo"):
    return await c.post("/v1/jobs", json={"kind": kind, "payload": {}}, headers=auth(key))


# ---- API admission --------------------------------------------------------------------------


async def test_rate_limit_429_with_retry_after_and_tenant_isolation(apool, api_keys):
    async with make_client(apool, FakeArq(), rate_per_minute=2) as c:
        assert [(await post(c, api_keys[A])).status_code for _ in range(2)] == [202, 202]
        r = await post(c, api_keys[A])
        assert r.status_code == 429 and r.json()["detail"]["error"] == "rate_limited"
        assert 1 <= int(r.headers["Retry-After"]) <= 60
        assert (await post(c, api_keys[B])).status_code == 202  # B unaffected by A's burst


async def test_active_job_cap_isolated_and_released(apool, api_keys, admin_url):
    async with make_client(apool, FakeArq(), max_active_jobs=2) as c:
        ids = [(await post(c, api_keys[A])).json()["job_id"] for _ in range(2)]
        r = await post(c, api_keys[A])
        assert r.status_code == 429 and r.json()["detail"]["error"] == "too_many_active_jobs"
        assert "Retry-After" in r.headers
        assert (await post(c, api_keys[B])).status_code == 202
        with psycopg.connect(admin_url) as conn:  # a job finishes: capacity frees up
            conn.execute("UPDATE jobs SET status='succeeded' WHERE job_id=%s", (ids[0],))
        assert (await post(c, api_keys[A])).status_code == 202


async def test_concurrent_submissions_never_exceed_active_cap(apool, api_keys, admin_url):
    async with make_client(apool, FakeArq(), max_active_jobs=3) as c:
        rs = await asyncio.gather(*[post(c, api_keys[A]) for _ in range(12)])
    assert sorted(r.status_code for r in rs).count(202) == 3
    with psycopg.connect(admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE tenant_id=%s", (A,)).fetchone()[0] == 3


async def test_daily_quota_blocks_provider_kinds_only_and_per_tenant(apool, api_keys, admin_url):
    set_usage(admin_url, A, llm=5)
    async with make_client(apool, FakeArq(), daily_llm_calls=5) as c:
        r = await post(c, api_keys[A], "lead_research")
        assert r.status_code == 429 and r.json()["detail"]["error"] == "daily_quota_exceeded"
        assert int(r.headers["Retry-After"]) >= 1
        assert (await post(c, api_keys[A], "echo")).status_code == 202      # free kind not blocked
        assert (await post(c, api_keys[B], "lead_research")).status_code == 202  # B has its own quota


async def test_search_quota_also_enforced(apool, api_keys, admin_url):
    set_usage(admin_url, A, searches=3)
    async with make_client(apool, FakeArq(), daily_searches=3) as c:
        assert (await post(c, api_keys[A], "lead_research")).status_code == 429


async def test_rate_limiter_unavailable_fails_closed_without_creating_a_job(apool, api_keys, admin_url):
    arq = FakeArq()
    arq.fail_incr = True
    async with make_client(apool, arq) as c:
        assert (await post(c, api_keys[A])).status_code == 503
    with psycopg.connect(admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE tenant_id=%s", (A,)).fetchone()[0] == 0


# ---- UsageRepo ------------------------------------------------------------------------------


async def test_usage_increments_are_atomic_under_concurrency(apool, api_keys):
    async def bump():
        async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
            await UsageRepo(s).add(TODAY, 2, 3)
    await asyncio.gather(*[bump() for _ in range(10)])
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        assert await UsageRepo(s).get(TODAY) == (20, 30)


async def test_usage_rls_blocks_cross_tenant_access(apool, api_keys):
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        await UsageRepo(s).add(TODAY, 4, 4)
    async with apool.connection() as conn, AsyncTenantSession(conn, B) as s:
        assert await UsageRepo(s).get(TODAY) == (0, 0)
        cur = await s.execute("SELECT count(*) FROM tenant_usage")
        assert (await cur.fetchone())[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # cannot write another tenant's row
            async with conn.transaction():
                await conn.execute("INSERT INTO tenant_usage (tenant_id, day) VALUES (%s, %s)", (A, TODAY))


# ---- worker ---------------------------------------------------------------------------------


class CountingHandler:
    """Stands in for the lead_research handler: makes a known number of provider calls."""

    def __init__(self, llm=2, searches=3, exc=None, sleep=0.0):
        self.llm, self.searches, self.exc, self.sleep = llm, searches, exc, sleep

    async def __call__(self, ctx, tenant_id, job_id, payload):
        for _ in range(self.searches):
            await ctx.search.search("q")
        for _ in range(self.llm):
            await ctx.llm.generate(None, "s", "u")
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.exc:
            raise self.exc
        return {"ok": True}


class NullLLM:
    async def generate(self, *_):
        return None


async def make_job(pool, tenant, kind="lead_research"):
    jid = uuid4()
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
        await JobRepo(s).create(jid, kind, {})
    return jid


async def get_job(pool, tenant, jid):
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
        return await JobRepo(s).get(jid)


async def usage(pool, tenant):
    async with pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
        return await UsageRepo(s).get(TODAY)


def worker_ctx(pool, **extra):
    search = FixtureSearch({}, [SearchResult("https://a.example", "t", "c")])
    return {"pool": pool, "search": search, "llm": NullLLM(), **extra}


async def test_usage_flushed_on_success_with_exact_counts(apool, monkeypatch):
    monkeypatch.setitem(JOB_HANDLERS, "lead_research", CountingHandler(llm=2, searches=3))
    jid = await make_job(apool, A)
    await run_job(worker_ctx(apool), A, str(jid))
    assert (await get_job(apool, A, jid)).status == "succeeded"
    assert await usage(apool, A) == (2, 3) and await usage(apool, B) == (0, 0)


async def test_usage_flushed_on_failure_and_error_is_redacted(apool, monkeypatch):
    register_secret("sk-live-supersecret-9999")
    monkeypatch.setitem(JOB_HANDLERS, "lead_research",
                        CountingHandler(exc=RuntimeError("401 bad key sk-live-supersecret-9999")))
    jid = await make_job(apool, A)
    await run_job(worker_ctx(apool), A, str(jid))
    job = await get_job(apool, A, jid)
    assert job.status == "failed" and "supersecret" not in job.error and "[REDACTED]" in job.error
    assert await usage(apool, A) == (2, 3)  # failed runs still cost money


async def test_usage_flushed_on_timeout(apool, monkeypatch):
    monkeypatch.setitem(JOB_HANDLERS, "lead_research", CountingHandler(llm=1, searches=1, sleep=5))
    jid = await make_job(apool, A)
    await run_job(worker_ctx(apool, run_timeout=0.2), A, str(jid))
    job = await get_job(apool, A, jid)
    assert job.status == "failed" and "timed out" in job.error
    assert await usage(apool, A) == (1, 1)


async def test_quota_rechecked_at_run_start_without_calling_providers(apool, monkeypatch, admin_url):
    handler = CountingHandler()
    monkeypatch.setitem(JOB_HANDLERS, "lead_research", handler)
    jid = await make_job(apool, A)  # queued before the limit was hit
    set_usage(admin_url, A, llm=9)
    ctx = worker_ctx(apool, limits=Limits(daily_llm_calls=9))
    await run_job(ctx, A, str(jid))
    job = await get_job(apool, A, jid)
    assert job.status == "failed" and job.error == "daily quota exceeded; resets at 00:00 UTC"
    assert ctx["search"].queries == [] and await usage(apool, A) == (9, 0)


async def test_free_kind_not_metered_or_quota_checked(apool, admin_url):
    set_usage(admin_url, A, llm=999, searches=999)
    jid = await make_job(apool, A, "echo")
    await run_job(worker_ctx(apool, limits=Limits(daily_llm_calls=1, daily_searches=1)), A, str(jid))
    assert (await get_job(apool, A, jid)).status == "succeeded"
    assert await usage(apool, A) == (999, 999)
