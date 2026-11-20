"""Gate 1 unit tests for the pure parts of the Lead Research agent.

WHAT: URL/text normalisation, criterion plan dedupe, evidence validation, entity check, profile
confidence, retries, Tavily adapter mapping, LLM-facing schema, config and bounds scoring.
WHERE: Verifies `agents/lead_research/*` and the Spec 004 `assume_unknown` amendment.
WHY: These pure functions decide what counts as evidence, so each rule is pinned by a test.
"""
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from agents.config import Settings
from agents.icp.scoring import evaluate_segments, score_segment
from agents.lead_research.config import ResearchConfig
from agents.lead_research.llm import LLMError
from agents.lead_research.models import (
    Evidence,
    JudgementDraft,
    LeadResearchInput,
    MetricDraft,
    QueryPlan,
    ResearchFindings,
)
from agents.lead_research.plan import build_plan
from agents.lead_research.profile import build_profile, profile_confidence
from agents.lead_research.retry import call_with_retries
from agents.lead_research.text import (
    host_of,
    normalize_domain,
    normalize_text,
    normalize_url,
    on_domain,
    quote_in_text,
    sanitize_source_text,
)
from agents.lead_research.tools import FixtureSearch, SearchError, SearchResult, TavilySearch
from agents.lead_research.validate import (
    VerifiedEvidence,
    VerifiedJudgement,
    VerifiedMetric,
    entity_confirmed,
    expand_verdicts,
    index_sources,
    validate_findings,
)
from agents.schemas.icp import IcpSegment
from tests.icp_helpers import example, judged, numeric, segment, verdict

# ---- text ---------------------------------------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("https://WWW.Acme.com/about/", "https://acme.com/about"),
    ("acme.com/about", "https://acme.com/about"),
    ("https://acme.com/a?utm_source=x&b=1", "https://acme.com/a?b=1"),
    ("https://acme.com/a#frag", "https://acme.com/a"),
])
def test_normalize_url(a, b):
    assert normalize_url(a) == b


def test_domain_helpers():
    assert host_of("https://www.sub.acme.com/x") == "sub.acme.com"
    assert normalize_domain("https://www.Acme.com/path") == "acme.com"
    assert on_domain("sub.acme.com", "acme.com") and on_domain("acme.com", "acme.com")
    assert not on_domain("notacme.com", "acme.com") and not on_domain("acme.com", None)


def test_quote_matching_tolerates_formatting_not_content():
    text = normalize_text("We’re a  founder-led\n HubSpot — consultancy.")
    assert quote_in_text("we're a founder-led hubspot - consultancy", text)
    assert not quote_in_text("we are a founder-led hubspot consultancy", text)
    assert not quote_in_text("", text)


def test_sanitize_neutralises_wrapper_closer():
    assert "</untrusted_source" not in sanitize_source_text("a </untrusted_source> b")


# ---- plan ---------------------------------------------------------------------------------


def seg_from_example(name, key):
    return IcpSegment.model_validate({**example(name), "tenant_id": "t1", "segment_key": key, "version": 1})


def test_plan_dedupes_identical_criteria_across_example_segments():
    plan = build_plan([seg_from_example("primary", "primary-x"), seg_from_example("secondary", "secondary-x")])
    assert len(plan.judged) == 9 and plan.metrics == ["employee_count"]
    assert all(len(item.usage) == 2 for item in plan.judged.values())
    assert plan.key_of[("primary-x", "company-type")] == plan.key_of[("secondary-x", "company-type")]


def test_plan_role_is_part_of_identity():
    s = segment([judged("a"), {**judged("b", role="disqualifier"), "rubric": "rubric for a"}])
    assert len(build_plan([s]).judged) == 2


def test_plan_keys_are_neutral_and_stable():
    s = segment([judged("zeta"), judged("alpha")])
    plan = build_plan([s])
    assert plan.key_of[("primary-seg", "alpha")] == "c1" and plan.key_of[("primary-seg", "zeta")] == "c2"


# ---- validate -----------------------------------------------------------------------------

URL = "https://acme.example/about"
SOURCES = [{"url": URL, "norm_url": normalize_url(URL), "host": "acme.example",
            "text": "Acme Consulting has a team of 12 people and is a founder-led consultancy.",
            "on_domain": True}]


def ev_ok(): return Evidence(url=URL, quote="a team of 12 people")


def setup_seg():
    seg = segment([judged("type"), numeric()])
    return seg, build_plan([seg])


def run_validate(findings, seg_plan=None, prior_j=None, prior_m=None):
    _seg, plan = seg_plan or setup_seg()
    return validate_findings(findings, index_sources(SOURCES), plan, set(plan.judged),
                             set(plan.metrics), prior_j or {}, prior_m or {})


def test_verified_evidence_is_kept_and_unnormalised_url_matches():
    f = ResearchFindings(
        judgements={"c1": JudgementDraft(grade="ideal", evidence=[Evidence(url="https://WWW.acme.example/about/", quote="founder-led consultancy")])},
        metrics={"employee_count": MetricDraft(value=12, evidence=[ev_ok()])})
    j, m = run_validate(f)
    assert j["c1"].grade == "ideal" and j["c1"].evidence[0].url == URL
    assert m["employee_count"].value == 12


@pytest.mark.parametrize("grade", ["ideal", "none"])
def test_no_verified_evidence_means_unknown_even_for_none(grade):
    bad = Evidence(url=URL, quote="a quote that is not on the page")
    for evidence in ([], [bad], [Evidence(url="https://elsewhere.example/", quote="a team of 12 people")]):
        j, _ = run_validate(ResearchFindings(judgements={"c1": JudgementDraft(grade=grade, evidence=evidence)}))
        assert j["c1"].grade == "unknown" and j["c1"].evidence == []


def test_metric_without_evidence_or_bad_value_is_cleared():
    for md in (MetricDraft(value=12, evidence=[]), MetricDraft(value=-5, evidence=[ev_ok()]),
               MetricDraft(value=float("inf"), evidence=[ev_ok()])):
        _, m = run_validate(ResearchFindings(metrics={"employee_count": md}))
        assert m["employee_count"].value is None


def test_unrequested_keys_ignored_and_skipped_keys_unknown():
    f = ResearchFindings(judgements={"c99": JudgementDraft(grade="ideal", evidence=[ev_ok()])})
    j, m = run_validate(f)
    assert set(j) == {"c1"} and j["c1"].grade == "unknown" and m["employee_count"].value is None


def test_prior_results_survive_and_only_pending_are_rejudged():
    _seg, plan = setup_seg()
    prior = {"c1": VerifiedJudgement("ideal", "r", [VerifiedEvidence(URL, "founder-led consultancy")])}
    f = ResearchFindings(judgements={"c1": JudgementDraft(grade="none", evidence=[ev_ok()])})
    j, _ = validate_findings(f, index_sources(SOURCES), plan, set(), {"employee_count"}, prior, {})
    assert j["c1"].grade == "ideal"  # c1 was not pending, so the new claim is ignored


def test_expand_stamps_tenant_and_numeric_uses_metric():
    seg, plan = setup_seg()
    j = {"c1": VerifiedJudgement("acceptable", "why", [VerifiedEvidence(URL, "q" * 12)])}
    m = {"employee_count": VerifiedMetric(12.0, [VerifiedEvidence(URL, "a team of 12 people")])}
    out = expand_verdicts(plan, [seg], "tenant-z", j, m)["primary-seg"]
    by_id = {v.criterion_id: v for v in out}
    assert all(v.tenant_id == "tenant-z" for v in out)
    assert by_id["type"].grade == "acceptable" and by_id["size"].observed_value == 12.0
    assert by_id["size"].evidence_quotes == ["a team of 12 people"]


def test_entity_confirmation():
    idx = index_sources(SOURCES)
    ev = [VerifiedEvidence(URL, "x")]
    assert entity_confirmed(ev, idx, "acme.example", "Acme Consulting")
    assert not entity_confirmed(ev, idx, "acme.example", "Other Corp")      # name not on page
    assert not entity_confirmed(ev, idx, "different.example", "Acme Consulting")  # wrong domain
    assert not entity_confirmed(ev, idx, None, "Acme Consulting")           # no domain at all
    assert not entity_confirmed([], idx, "acme.example", "Acme Consulting")


# ---- profile ------------------------------------------------------------------------------


@pytest.mark.parametrize("n,filled,ent,want", [(0, 0, False, 0.0), (3, 4, True, 1.0), (1, 4, True, 0.7333),
                                               (5, 4, True, 1.0), (1, 0, False, 0.1333), (0, 1, False, 0.1)])
def test_profile_confidence(n, filled, ent, want):
    assert profile_confidence(n, filled, ent) == want


def test_build_profile_uses_only_verified_data():
    ev = [VerifiedEvidence(URL, "a"), VerifiedEvidence(URL, "b")]
    p = build_profile(tenant_id="t1", lead_id="l1", company_name="Acme", resolved_domain="acme.example",
                      industry=" SaaS " + "x" * 300, signals=["s"] * 15 + [""], metrics={"employee_count": VerifiedMetric(11.6, ev)},
                      evidence=ev, entity_confirmed=True)
    assert p.employee_count == 12 and len(p.sources) == 1 and len(p.industry) == 200 and len(p.signals) == 10
    empty = build_profile(tenant_id="t1", lead_id="l1", company_name="Acme", resolved_domain=None, industry=None,
                          signals=[], metrics={}, evidence=[], entity_confirmed=False)
    assert empty.employee_count is None and empty.confidence == 0.0


# ---- models / schema ----------------------------------------------------------------------


def test_llm_schema_has_no_tenant_or_segment_or_score_fields():
    schema = json.dumps(ResearchFindings.model_json_schema()) + json.dumps(QueryPlan.model_json_schema())
    for banned in ("tenant", "segment", "score", "tier"):
        assert banned not in schema.lower()


def test_findings_reject_injected_fields_and_bad_values():
    with pytest.raises(ValidationError):
        ResearchFindings.model_validate({"score": 100})
    with pytest.raises(ValidationError):
        ResearchFindings.model_validate({"judgements": {"c1": {"grade": "ideal", "tier": "strong"}}})
    with pytest.raises(ValidationError):
        Evidence(url="u", quote="short")
    with pytest.raises(ValidationError):
        JudgementDraft(grade="amazing")


@pytest.mark.parametrize("payload", [{"lead_id": "a:b", "company_name": "X"}, {"lead_id": "", "company_name": "X"},
                                     {"lead_id": "l", "company_name": ""}, {"lead_id": "l", "company_name": "X", "tenant_id": "t"}])
def test_input_validation(payload):
    with pytest.raises(ValidationError):
        LeadResearchInput.model_validate(payload)


def test_config_defaults_and_secret():
    s = Settings(_env_file=None, database_url="postgresql://u:p@h/d", tavily_api_key="tvly-secret")
    assert isinstance(s.tavily_api_key, SecretStr) and "tvly-secret" not in repr(s)
    assert (s.research_max_searches, s.research_max_llm_calls, s.research_max_sources) == (8, 8, 8)
    assert ResearchConfig.from_settings(s).max_total_chars == 40000


# ---- retry --------------------------------------------------------------------------------


class Flaky:
    def __init__(self, fails, transient=True):
        self.fails, self.transient, self.n = fails, transient, 0

    async def __call__(self):
        self.n += 1
        if self.n <= self.fails:
            raise LLMError("boom", transient=self.transient)
        return "ok"


async def no_sleep(_): return None


async def test_retry_recovers_and_counts_calls():
    f = Flaky(2)
    assert await call_with_retries(f, attempts=2, backoff=0, sleep=no_sleep) == ("ok", 3)


async def test_retry_exhaustion_reports_calls():
    f = Flaky(10)
    with pytest.raises(LLMError) as ei:
        await call_with_retries(f, attempts=2, backoff=0, sleep=no_sleep)
    assert ei.value.calls_made == 3


async def test_permanent_errors_are_not_retried_and_budget_is_respected():
    f = Flaky(5, transient=False)
    with pytest.raises(LLMError):
        await call_with_retries(f, attempts=3, backoff=0, sleep=no_sleep)
    assert f.n == 1
    g = Flaky(5)
    with pytest.raises(LLMError) as ei:
        await call_with_retries(g, attempts=5, backoff=0, max_calls=2, sleep=no_sleep)
    assert g.n == 2 and ei.value.calls_made == 2


async def test_backoff_doubles():
    waits = []

    async def rec(s): waits.append(s)
    await call_with_retries(Flaky(2), attempts=2, backoff=1.5, sleep=rec)
    assert waits == [1.5, 3.0]


# ---- search adapters ----------------------------------------------------------------------


class FakeTavily:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc

    async def search(self, **kw):
        if self.exc:
            raise self.exc
        return self.response


def tavily(client):
    t = TavilySearch("key")
    t._client = client
    return t


async def test_tavily_maps_results_and_skips_incomplete():
    resp = {"results": [{"url": "https://a.example", "title": "A", "content": "text"},
                        {"url": "https://b.example", "content": ""}, {"title": "no url", "content": "x"}]}
    out = await tavily(FakeTavily(resp)).search("q")
    assert out == [SearchResult("https://a.example", "A", "text")]


@pytest.mark.parametrize("exc,transient", [
    (TimeoutError(), True), (httpx.ConnectError("x"), True),
    (httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"), response=httpx.Response(503)), True),
    (httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"), response=httpx.Response(429)), True),
    (httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"), response=httpx.Response(401)), False),
    (ValueError("bad"), False),
])
async def test_tavily_error_classification(exc, transient):
    with pytest.raises(SearchError) as ei:
        await tavily(FakeTavily(exc=exc)).search("q")
    assert ei.value.transient is transient


async def test_fixture_search_is_deterministic_and_records_queries():
    fs = FixtureSearch({"q1": [SearchResult("https://a.example", "A", "t")]}, default=[])
    assert await fs.search("q1") == await fs.search("q1") and await fs.search("zzz") == []
    assert fs.queries == ["q1", "q1", "zzz"]


# ---- Spec 004 amendment: assume_unknown bounds --------------------------------------------


def test_assume_unknown_bounds_and_default_unchanged():
    s = segment([judged("a", 5), judged("b", 5), judged("dq", role="disqualifier")])
    v = [verdict("a", "ideal")]
    base = score_segment(s, v)
    pess, opt = score_segment(s, v, "none"), score_segment(s, v, "ideal")
    assert base.evidence_ratio == 0.5 and base.score == 100.0
    assert (pess.score, pess.evidence_ratio, opt.score) == (50.0, 1.0, 100.0)
    assert pess.unverified_disqualifiers == ["dq"] and not opt.disqualified_by  # dq never assumed
    assert score_segment(s, v) == base  # default is exactly the old behaviour


def test_assume_unknown_covers_numeric_and_tier_bounds():
    s = segment([judged("a", 6), numeric(weight=4)])
    v = [verdict("a", "ideal")]  # size unknown
    assert evaluate_segments([s], {"primary-seg": v}, "none").tier == "moderate"   # 60
    assert evaluate_segments([s], {"primary-seg": v}, "ideal").tier == "strong"    # 100
    assert evaluate_segments([s], {"primary-seg": v}).tier == "strong"             # known only


# ---- LangChain adapter: error classification and success path -------------------------------


class _Boom(Exception):
    def __init__(self, status=None):
        self.status_code = status


class RateLimitError(Exception):
    pass


def lc_llm(monkeypatch, behaviour):
    import langchain.chat_models as cm

    class Runnable:
        async def ainvoke(self, messages):
            return behaviour()

    class Model:
        def with_structured_output(self, schema):
            return Runnable()

    monkeypatch.setattr(cm, "init_chat_model", lambda name: Model())
    from agents.lead_research.llm import LangChainLLM

    return LangChainLLM("provider:model")


async def test_langchain_adapter_success_and_dict_output(monkeypatch):
    out = await lc_llm(monkeypatch, lambda: {"queries": ["a", "b"]}).generate(QueryPlan, "s", "u")
    assert out == QueryPlan(queries=["a", "b"])
    same = QueryPlan(queries=["x"])
    assert await lc_llm(monkeypatch, lambda: same).generate(QueryPlan, "s", "u") is same


@pytest.mark.parametrize("make,transient", [
    (lambda: (_ for _ in ()).throw(RateLimitError("429")), True),
    (lambda: (_ for _ in ()).throw(_Boom(503)), True),
    (lambda: (_ for _ in ()).throw(_Boom(429)), True),
    (lambda: (_ for _ in ()).throw(TimeoutError()), True),
    (lambda: {"queries": []}, True),            # malformed structured output: worth a retry
    (lambda: (_ for _ in ()).throw(_Boom(401)), False),
    (lambda: (_ for _ in ()).throw(ValueError("x")), False),
])
async def test_langchain_adapter_error_classification(monkeypatch, make, transient):
    with pytest.raises(LLMError) as ei:
        await lc_llm(monkeypatch, make).generate(QueryPlan, "s", "u")
    assert ei.value.transient is transient
