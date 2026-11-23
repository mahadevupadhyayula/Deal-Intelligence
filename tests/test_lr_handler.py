"""Gate 1/2 tests for the `lead_research` handler and job integration.

WHAT: Handler wiring (real DB, scripted LLM): success, no active ICP, bad payload, missing
configuration, run timeout (job not left `running`), LLM-call cap, two-tenant isolation,
thread id, and API/worker separation.
WHERE: Verifies `agents/lead_research/handler.py`, `workers/tasks.py`, `workers/handlers.py`.
WHY: These are the failure modes that cost money or corrupt state in production.
"""
import asyncio
import re
import subprocess
import sys
from uuid import UUID, uuid4

import pytest

from agents.jobs import JobContext
from agents.lead_research.graph import build_graph
from agents.lead_research.handler import lead_research
from agents.lead_research.nodes import NoActiveIcpError, ResearchDeps
from agents.repos.async_session import AsyncTenantSession
from agents.repos.evaluation import AsyncEvaluationRepo
from agents.repos.job import JobRepo
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
from tests.lr_helpers import (
    ScriptedLLM,
    build_config,
    build_search,
    initial_state,
    load_fixture,
    seed_icp,
)
from workers.tasks import run_job

HAPPY = load_fixture("strong_primary_fit")
INP = HAPPY["input"]


def parts(llm=None, fixture=HAPPY):
    llm = llm or ScriptedLLM(fixture["llm"]["plans"], fixture["llm"]["synths"])
    return build_search(fixture["search"]), llm


async def test_handler_success_result_shape(apool):
    await seed_icp(apool, A, HAPPY["icp"])
    search, llm = parts()
    out = await lead_research(JobContext(apool, search, llm), A, str(uuid4()), INP, build_config({}))
    assert out["status"] == "complete" and out["evaluation"]["tier"] == "strong"
    assert out["profile"]["employee_count"] == 12 and out["evaluation"]["entity_confirmed"] is True
    assert out["evaluation"]["icp_versions"] == {"primary-seg": 1, "secondary-seg": 1}


async def test_no_active_icp_fails_before_any_search_or_llm_call(apool):
    search, llm = parts()
    deps = ResearchDeps(apool, search, llm, build_config({}))
    with pytest.raises(NoActiveIcpError, match="no active ICP defined"):
        await build_graph(deps).ainvoke(initial_state(A, INP))
    assert llm.calls == [] and search.queries == []


async def test_no_active_icp_through_run_job_records_message(apool):
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        jid = uuid4()
        await JobRepo(s).create(jid, "lead_research", INP)
    search, llm = parts()
    await run_job({"pool": apool, "search": search, "llm": llm}, A, str(jid))
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        job = await JobRepo(s).get(jid)
    assert job.status == "failed" and "no active ICP defined" in job.error and llm.calls == []


@pytest.mark.parametrize("payload,needle", [
    ({"lead_id": "a:b", "company_name": "X"}, "lead_id"),
    ({"lead_id": "l"}, "company_name"),
    ({"lead_id": "l", "company_name": "X", "tenant_id": "t"}, "tenant_id"),
])
async def test_invalid_payload_fails_job_with_message(apool, payload, needle):
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        jid = uuid4()
        await JobRepo(s).create(jid, "lead_research", payload)
    search, llm = parts()
    await run_job({"pool": apool, "search": search, "llm": llm}, A, str(jid))
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        job = await JobRepo(s).get(jid)
    assert job.status == "failed" and needle in job.error and llm.calls == []


async def test_unconfigured_worker_fails_job_clearly(apool):
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        jid = uuid4()
        await JobRepo(s).create(jid, "lead_research", INP)
    await run_job({"pool": apool}, A, str(jid))  # no search/llm in ctx
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        job = await JobRepo(s).get(jid)
    assert job.status == "failed" and "not configured" in job.error


async def test_run_timeout_marks_job_failed_not_stuck_running(apool):
    await seed_icp(apool, A, HAPPY["icp"])
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        jid = uuid4()
        await JobRepo(s).create(jid, "lead_research", INP)
    search, llm = parts(ScriptedLLM(HAPPY["llm"]["plans"], HAPPY["llm"]["synths"], delay=2.0))
    await run_job({"pool": apool, "search": search, "llm": llm, "run_timeout": 0.1}, A, str(jid))
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        job = await JobRepo(s).get(jid)
    assert job.status == "failed" and job.error == "timed out after 0.1s"


async def test_llm_call_cap_skips_synthesis_and_is_partial(apool):
    await seed_icp(apool, A, HAPPY["icp"])
    search, llm = parts()
    graph = build_graph(ResearchDeps(apool, search, llm, build_config({"max_llm_calls": 1})))
    final = await graph.ainvoke(initial_state(A, INP))
    assert [n for n, _ in llm.calls] == ["QueryPlan"]           # plan used the only call
    assert final["evaluation"].status == "partial"
    assert final["evaluation"].tier == "insufficient_data" and final["llm_calls_used"] == 1


async def test_two_tenants_get_separate_icps_and_results(apool):
    fx_b = load_fixture("refine_skipped_when_tier_fixed")  # matches B's ICP (extra criterion)
    await seed_icp(apool, A, HAPPY["icp"])
    await seed_icp(apool, B, fx_b["icp"])
    runs = []
    for tenant, fx in ((A, HAPPY), (B, fx_b)):
        s, llm = parts(fixture=fx)
        graph = build_graph(ResearchDeps(apool, s, llm, build_config({})))
        runs.append(graph.ainvoke(initial_state(tenant, INP)))
    fa, fb = await asyncio.gather(*runs)
    assert {v.tenant_id for vs in fa["evaluation"].verdicts.values() for v in vs} == {A}
    assert {v.tenant_id for vs in fb["evaluation"].verdicts.values() for v in vs} == {B}
    assert len(fa["plan"].judged) != len(fb["plan"].judged)
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        assert (await AsyncEvaluationRepo(s).get_latest(INP["lead_id"])).tenant_id == A
    async with apool.connection() as conn, AsyncTenantSession(conn, B) as s:
        assert (await AsyncEvaluationRepo(s).get_latest(INP["lead_id"])).tenant_id == B


async def test_same_job_persisted_twice_creates_one_evaluation(apool):
    await seed_icp(apool, A, HAPPY["icp"])
    job_id = uuid4()
    for _ in range(2):
        s, llm = parts()
        graph = build_graph(ResearchDeps(apool, s, llm, build_config({})))
        await graph.ainvoke(initial_state(A, INP, job_id))
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        assert len(await AsyncEvaluationRepo(s).list(INP["lead_id"])) == 1


async def test_thread_id_is_tenant_job_scoped(apool, monkeypatch):
    await seed_icp(apool, A, HAPPY["icp"])
    seen = {}
    import agents.lead_research.handler as h

    real = h.build_graph

    def spy(deps, checkpointer=None):
        g = real(deps, checkpointer)
        orig = g.ainvoke

        async def ainvoke(state, config=None, **kw):
            seen.update(config["configurable"], meta=config["metadata"])
            return await orig(state, config, **kw)

        g.ainvoke = ainvoke
        return g

    monkeypatch.setattr(h, "build_graph", spy)
    jid = str(uuid4())
    search, llm = parts()
    await lead_research(JobContext(apool, search, llm), A, jid, INP, build_config({}))
    assert seen["thread_id"] == f"{A}:l1:{jid}"
    assert A not in str(seen["meta"]) and len(seen["meta"]["tenant_hash"]) == 12  # raw tenant id not sent
    UUID(seen["meta"]["job_id"])


def test_api_process_does_not_import_agent_libraries():
    code = ("import sys; import api.main; "
            "bad=[m for m in ('langgraph','langchain','tavily') if m in sys.modules]; "
            "print('BAD:'+','.join(bad) if bad else 'CLEAN')")
    env_py = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False,
                            env={**__import__("os").environ, "DATABASE_URL": "postgresql://u:p@localhost/d"})
    assert env_py.stdout.strip() == "CLEAN", env_py.stdout + env_py.stderr


async def test_prompt_size_caps_and_every_source_is_wrapped(apool):
    from agents.lead_research.tools import FixtureSearch, SearchResult

    await seed_icp(apool, A, HAPPY["icp"])
    many = [SearchResult(f"https://site{i}.example/p", f"T{i}", "Acme Consulting " + "x" * 5000) for i in range(20)]
    llm = ScriptedLLM([{"queries": ["q1"]}], [{"industry": None, "signals": [], "metrics": {}, "judgements": {}}])
    graph = build_graph(ResearchDeps(apool, FixtureSearch({}, many), llm,
                                     build_config({"max_sources": 3, "max_total_chars": 7000, "max_refine_loops": 0})))
    final = await graph.ainvoke(initial_state(A, INP))
    prompt = next(u for n, u in llm.calls if n == "ResearchFindings")
    assert len(final["sources"]) <= 3 and sum(len(s["text"]) for s in final["sources"]) <= 7000
    assert (len(re.findall(r"<untrusted_source_[0-9a-f]{16} id=", prompt)) == len(final["sources"])
            == len(re.findall(r"</untrusted_source_[0-9a-f]{16}>", prompt)))


async def test_company_domain_sources_are_ranked_first(apool):
    from agents.lead_research.tools import FixtureSearch, SearchResult

    await seed_icp(apool, A, HAPPY["icp"])
    results = [SearchResult("https://blog.example/a", "B", "Acme Consulting blog"),
               SearchResult("https://acme-consulting.example/team", "T", "Acme Consulting team")]
    llm = ScriptedLLM([{"queries": ["q1"]}], [{"industry": None, "signals": [], "metrics": {}, "judgements": {}}])
    graph = build_graph(ResearchDeps(apool, FixtureSearch({}, results), llm,
                                     build_config({"max_sources": 1, "max_refine_loops": 0})))
    final = await graph.ainvoke(initial_state(A, INP))
    assert [s["host"] for s in final["sources"]] == ["acme-consulting.example"]
