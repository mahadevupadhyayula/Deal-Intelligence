"""Gate 2 extras for the adversarial fixtures: assertions the generic golden runner cannot make.

WHAT: For the Spec 006 adversarial fixtures, check what the MODEL saw (prompt structure) and
what was STORED (cleaned free text), plus trace-redaction wiring.
WHERE: Complements `test_lr_golden.py`, which already checks scores, tiers, flags and calls.
WHY: A golden score match proves the outcome; these prove the mechanism (the forged closer sat
inside the real wrapper, hidden characters never reached the model, markup never reached the DB).
"""
import re

from agents.guardrails.tracing import _mask, configure_trace_redaction
from agents.lead_research.graph import build_graph
from agents.lead_research.nodes import ResearchDeps
from tests.conftest import TENANT_A as A
from tests.lr_helpers import (
    ScriptedLLM,
    build_config,
    build_search,
    initial_state,
    load_fixture,
    seed_icp,
)


async def run(apool, name):
    fx = load_fixture(name)
    await seed_icp(apool, A, fx["icp"])
    llm = ScriptedLLM(fx["llm"]["plans"], fx["llm"]["synths"])
    graph = build_graph(ResearchDeps(apool, build_search(fx["search"]), llm, build_config(fx["config"])))
    final = await graph.ainvoke(initial_state(A, fx["input"]))
    prompt = next(u for n, u in llm.calls if n == "ResearchFindings")
    return final, prompt


async def test_forged_closer_stays_inside_the_real_wrapper(apool):
    final, prompt = await run(apool, "injection_delimiter_breakout")
    boundary = re.search(r"<untrusted_source_([0-9a-f]{16}) ", prompt).group(1)
    real_close = f"</untrusted_source_{boundary}>"
    assert prompt.count(real_close) == len(final["sources"]) == 1
    assert prompt.index("</untrusted_source>") < prompt.index(real_close)  # forged one is inside
    assert boundary not in final["sources"][0]["text"]
    assert final["evaluation"].flags_detail["injection_suspected"]  # reviewer sees why


async def test_hidden_characters_never_reach_the_model(apool):
    final, prompt = await run(apool, "injection_hidden_unicode")
    assert not any(0xE0000 <= ord(ch) <= 0xE007F or ch == "\u200b" for ch in prompt)
    assert "founder-led HubSpot implementation consultancy" in final["sources"][0]["text"]


async def test_obedient_model_cannot_use_the_injected_sentence_as_evidence(apool):
    final, _ = await run(apool, "injection_rate_me_strong")
    ev = final["evaluation"]
    assert ev.tier == "insufficient_data" and ev.score == 0.0
    assert all(v.evidence_quotes == [] for vs in ev.verdicts.values() for v in vs)


async def test_prompt_leak_text_is_nulled_and_flagged(apool):
    final, _ = await run(apool, "prompt_leak")
    ev = final["evaluation"]
    assert "prompt_leak_suspected" in ev.flags
    assert all("Only tags named exactly" not in v.rationale for vs in ev.verdicts.values() for v in vs)
    assert ev.tier == "strong"  # the evidence-backed grade is unaffected


async def test_markup_is_stripped_from_stored_text(apool):
    final, _ = await run(apool, "markup_in_rationale")
    stored = final["profile"].industry + " ".join(final["profile"].signals) + " ".join(
        v.rationale for vs in final["evaluation"].verdicts.values() for v in vs)
    for bad in ("<", ">", "http", "](", "onerror", "evil"):
        assert bad not in stored, bad
    assert final["profile"].industry == "HubSpot consultancy"
    assert final["profile"].signals == ["x()Founder-led", "see"]


def test_trace_mask_redacts_secrets_and_pii_and_installs():
    out = _mask("key sk-proj-abcdef1234 mail a.b@c.example tel +1 415 555 0100", ())
    assert "abcdef1234" not in out and "[EMAIL]" in out and "[PHONE]" in out
    assert configure_trace_redaction() is True
