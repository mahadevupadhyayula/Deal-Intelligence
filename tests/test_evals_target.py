"""Gate 1/2 tests for the Lead Research eval adapter (Spec 007): needs the test database.

WHAT: Runs the existing golden fixtures through the eval runner in `scripted` mode and checks
they reproduce Gate 2; verifies the outcome shape scorers rely on, per-run tenant creation and
cleanup (on success, error and timeout), and that an eval never reads or touches another tenant.
WHERE: Verifies `agents/evals/targets/lead_research.py` + `runner.py` against real Postgres/RLS.
WHY: The harness creates and deletes tenants through the admin role; a bug there could leak
rows or delete the wrong tenant, so isolation and cleanup are tested explicitly.
"""
import psycopg
import pytest
from pydantic import SecretStr

from agents.config import get_settings
from agents.evals.runner import run_suite
from agents.evals.scripted import ScriptedLLM
from agents.evals.targets.lead_research import LeadResearchTarget, load_suite
from agents.lead_research.config import ResearchConfig
from agents.lead_research.tools import FixtureSearch
from tests.conftest import TENANT_A as A
from tests.lr_helpers import seed_icp


@pytest.fixture
def target_settings(migrated_test_db, admin_url):
    return get_settings().model_copy(update={
        "database_url": SecretStr(migrated_test_db), "admin_database_url": SecretStr(admin_url)})


def eval_tenants(admin_url) -> int:
    with psycopg.connect(admin_url) as c:
        return c.execute("SELECT count(*) FROM tenants WHERE tenant_id LIKE 'eval-%%'").fetchone()[0]


async def test_golden_suite_reproduces_gate2_through_the_runner(target_settings, admin_url):
    before = eval_tenants(admin_url)
    cases = load_suite("golden")
    rep = await run_suite(LeadResearchTarget(target_settings), cases, suite="golden",
                          mode="scripted", concurrency=4)
    failed = [(r.case_id, [c.detail for c in r.checks if not c.passed]) for r in rep.results
              if not r.passed]
    assert failed == [] and len(rep.results) == 22
    assert eval_tenants(admin_url) == before  # every throwaway tenant was deleted


async def test_outcome_shape_for_scorers(target_settings):
    (c,) = [x for x in load_suite("golden") if x.id == "strong_primary_fit"]
    t = LeadResearchTarget(target_settings)
    await t.setup()
    try:
        run = await t.run_case(c, 0, "scripted")
    finally:
        await t.teardown()
    o = run.outcome
    assert run.error is None and o["tier"] == "strong" and o["unverified_quotes"] == []
    assert o["quotes"] and o["canary"].startswith("CANARY-") and o["employee_count"] == 12
    assert o["caps"]["max_llm_calls"] > 0 and o["_source_texts"] and "primary-seg" in o["grades"]
    assert run.llm_calls == 2 and run.searches == 2  # metered like production


async def test_error_run_is_recorded_and_tenant_still_deleted(target_settings, admin_url):
    (c,) = [x for x in load_suite("golden") if x.id == "strong_primary_fit"]
    broken = c.model_copy(update={"scripted": {"llm": {"plans": [], "synths": []}, "config": {}}})
    before = eval_tenants(admin_url)
    rep = await run_suite(LeadResearchTarget(target_settings), [broken], suite="golden",
                          mode="scripted")
    assert rep.results[0].errors == 1 and not rep.results[0].passed
    assert eval_tenants(admin_url) == before


async def test_timeout_is_recorded_with_spend_and_tenant_deleted(target_settings, admin_url):
    (c,) = [x for x in load_suite("golden") if x.id == "strong_primary_fit"]

    class Slow(LeadResearchTarget):
        def _deps(self, case, mode):
            search = FixtureSearch({}, [])
            return search, ScriptedLLM(plans=[{"queries": ["q"]}], delay=2), ResearchConfig(
                retry_backoff_seconds=0.0)

    before = eval_tenants(admin_url)
    t = Slow(target_settings, timeout_s=0.2)
    await t.setup()
    try:
        run = await t.run_case(c, 0, "scripted")
    finally:
        await t.teardown()
    assert "timeout" in run.error and run.llm_calls == 1  # the attempted call is still counted
    assert eval_tenants(admin_url) == before


async def test_eval_never_touches_other_tenants(target_settings, apool, admin_url):
    await seed_icp(apool, A, "icp_mini.json")
    with psycopg.connect(admin_url) as c:
        icp_before = c.execute("SELECT count(*) FROM icp_segments WHERE tenant_id = %s",
                               (A,)).fetchone()[0]
    rep = await run_suite(LeadResearchTarget(target_settings), load_suite("golden")[:4],
                          suite="golden", mode="scripted")
    assert all(r.passed for r in rep.results)
    with psycopg.connect(admin_url) as c:
        assert c.execute("SELECT count(*) FROM icp_segments WHERE tenant_id = %s",
                         (A,)).fetchone()[0] == icp_before > 0
        assert c.execute("SELECT count(*) FROM leads WHERE tenant_id = %s", (A,)).fetchone()[0] == 0
    # the case-supplied tenant is ignored: results cannot depend on tenant A's ICP/leads
    assert not any(r.runs[0].outcome.get("flags_detail", {}).get("tenant") for r in rep.results)
