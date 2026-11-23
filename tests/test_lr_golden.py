"""Gate 2: golden-dataset trajectories for the Lead Research graph.

WHAT: Runs the real LangGraph pipeline (real Postgres, scripted LLM, fixture search) over each
fixture in `tests/fixtures/lead_research/` and compares node sequence, queries, evaluation,
verdicts, flags and profile to the hand-computed expectations in the fixture.
WHERE: Verifies `agents/lead_research/` end to end, including persistence and read-back.
WHY: With a scripted LLM this proves orchestration, evidence verification, scoring, budgets and
persistence. It does NOT prove a real model judges well (see Spec 005, Gate 3).
"""
import json
import re

import pytest

from agents.lead_research.graph import build_graph
from agents.lead_research.nodes import ResearchDeps
from agents.repos.async_session import AsyncTenantSession
from agents.repos.evaluation import AsyncEvaluationRepo
from agents.repos.lead_async import AsyncLeadRepo
from tests.conftest import TENANT_A as A
from tests.lr_helpers import (
    ScriptedLLM,
    build_config,
    build_search,
    fixture_names,
    initial_state,
    load_fixture,
    seed_icp,
)


@pytest.mark.parametrize("name", fixture_names())
async def test_golden_case(apool, name):
    fx = load_fixture(name)
    exp = fx["expect"]
    await seed_icp(apool, A, fx["icp"])
    search, llm = build_search(fx["search"]), ScriptedLLM(fx["llm"]["plans"], fx["llm"]["synths"])
    graph = build_graph(ResearchDeps(apool, search, llm, build_config(fx["config"])))

    final = await graph.ainvoke(initial_state(A, fx["input"]))
    ev, profile = final["evaluation"], final["profile"]

    assert final["trace"] == exp["nodes"]
    assert search.queries == exp["queries"]
    assert (ev.tier, ev.score) == (exp["tier"], exp["score"])
    assert (ev.best_segment_key, ev.best_rank) == (exp["best_segment_key"], exp["best_rank"])
    assert ev.status == exp["status"]
    assert sorted(ev.flags) == sorted(exp["flags"])
    assert ev.entity_confirmed is exp["entity_confirmed"]
    assert final["llm_calls_used"] == exp["llm_calls"] == len(llm.calls)
    assert final["searches_used"] == exp["searches"]

    for seg_key, want in exp.get("segments", {}).items():
        got = next(s for s in ev.segments if s.segment_key == seg_key)
        assert (got.score, got.tier) == (want["score"], want["tier"]), seg_key
        assert got.disqualified_by == want.get("disqualified_by", [])
    for seg_key, grades in exp.get("grades", {}).items():
        by_id = {v.criterion_id: v for v in ev.verdicts[seg_key]}
        for cid, grade in grades.items():
            assert by_id[cid].grade == grade, (seg_key, cid)
            if grade == "unknown":
                assert by_id[cid].evidence_quotes == []
    for seg_key, observed in exp.get("observed", {}).items():
        by_id = {v.criterion_id: v for v in ev.verdicts[seg_key]}
        for cid, value in observed.items():
            assert by_id[cid].observed_value == value
    for field, want in exp.get("profile", {}).items():
        got = len(profile.sources) if field == "source_count" else getattr(profile, field)
        assert got == want, field

    # Every verified quote really is in its cited source (the audit property).
    texts = {s["url"]: s["text"] for s in final["sources"]}
    for verdicts in ev.verdicts.values():
        for v in verdicts:
            for url, quote in zip(v.evidence_urls, v.evidence_quotes, strict=False):
                assert " ".join(quote.split()).casefold() in " ".join(texts[str(url)].split()).casefold()

    synth_prompts = [u for n, u in llm.calls if n == "ResearchFindings"]
    if "second_synth_excludes" in exp:
        second = synth_prompts[1]
        assert all(f"- {k}" not in second for k in exp["second_synth_excludes"])
        assert all(k in second for k in exp["second_synth_includes"])
    if "synth_criteria_lines" in exp:
        crit = synth_prompts[0].split("CRITERIA to judge")[1].split("SOURCES:")[0]
        assert sum(1 for ln in crit.splitlines() if ln.startswith("- c")) == exp["synth_criteria_lines"]
    if exp.get("closer_count_equals_sources"):
        assert len(re.findall(r"</untrusted_source_[0-9a-f]{16}>", synth_prompts[0])) == len(final["sources"])
    dumped = json.dumps([ev.model_dump(mode="json"), profile.model_dump(mode="json")])
    for bad in exp.get("forbidden_strings", []):
        assert bad not in dumped

    # Persistence round trip: what the graph returned is what is in the database.
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        assert await AsyncEvaluationRepo(s).get_latest(fx["input"]["lead_id"]) == ev
        assert await AsyncLeadRepo(s).get(fx["input"]["lead_id"]) == profile
