"""Lead Research eval adapter and dataset suites.

WHAT: `LeadResearchTarget` runs the REAL research graph once per (case, repeat) under a
throwaway tenant and returns a JSON-safe outcome; `load_suite` maps suite names to datasets.
WHERE: The only agent-specific module in the eval package; used by the CLI.
WHY: Each run gets its own tenant (`eval-<random>`), created and deleted through the admin
role, so runs are isolated from each other and from real tenants, concurrent runs cannot clash
on ICP versions, and cleanup is a delete by tenant id. Everything else is production wiring: the
same graph, the same metering decorators and the same guardrails, so a result describes what
production would do. Three modes: `scripted` (scripted LLM + fixture search), `frozen` (REAL
model + pinned search results: reproducible inputs, noisy model) and `live` (real model + real
Tavily). The outcome carries only what scorers need; bulky private data uses `_`-prefixed keys
that the report writer drops.
"""
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import psycopg_pool

from agents.config import Settings
from agents.evals.loader import load_cases
from agents.evals.models import CaseRun, CheckSpec, EvalCase
from agents.evals.scripted import ScriptedLLM
from agents.guardrails.redact import redact
from agents.guardrails.usage import MeteredLLM, MeteredSearch, UsageMeter
from agents.lead_research.config import ResearchConfig
from agents.lead_research.graph import build_graph
from agents.lead_research.llm import LangChainLLM
from agents.lead_research.nodes import ResearchDeps
from agents.lead_research.text import normalize_text, quote_in_text
from agents.lead_research.tools import FixtureSearch, SearchResult, TavilySearch
from agents.repos.async_session import AsyncTenantSession
from agents.repos.icp import IcpRepo
from agents.schemas.icp import IcpSegmentIn

ROOT = Path(__file__).resolve().parents[3]
DATASETS = ROOT / "evals" / "datasets" / "lead_research"
GOLDEN = ROOT / "tests" / "fixtures" / "lead_research"
DEFAULT_ICP = "icp.json"

# suite -> default mode. Calibration is the only suite that defaults to the real web.
SUITE_MODES = {"golden": "scripted", "adversarial": "frozen", "judgement": "frozen",
               "calibration": "live"}
FIT_TIERS, NOT_FIT_TIERS = ["strong", "moderate"], ["weak", "not_fit"]


def _golden_cases() -> list[EvalCase]:
    """The existing Gate 2 fixtures, exposed as scripted cases (no duplicated files)."""
    cases = []
    for p in sorted(GOLDEN.glob("*.json")):
        if p.stem.startswith("icp_"):
            continue
        fx = json.loads(p.read_text())
        cases.append(EvalCase(
            id=fx["name"], suite="golden", description=fx.get("description", ""),
            input=fx["input"], icp=str(GOLDEN / fx["icp"]), corpus=fx["search"],
            scripted={"llm": fx["llm"], "config": fx.get("config", {})},
            checks=[CheckSpec(name="golden_expect", args={"expect": fx["expect"]})]))
    return cases


def _calibration_cases() -> list[EvalCase]:
    path = DATASETS / "calibration" / "labelled_companies.json"
    if not path.exists():
        return []
    icp = DATASETS / "calibration" / "icp.json"
    cases = []
    for i, row in enumerate(json.loads(path.read_text())):
        label = row["label"]
        assert label in ("fit", "not_fit"), f"bad label {label!r}"
        cases.append(EvalCase(
            id=f"cal-{i:02d}-{row['company_name'].lower().replace(' ', '-')[:30]}",
            suite="calibration", tags=["calibration", f"label:{label}"], gating=False,
            input={"company_name": row["company_name"], "domain": row.get("domain")},
            icp=str(icp) if icp.exists() else None, repeats=1,
            checks=[CheckSpec(name="tier_in", args={
                "allowed": FIT_TIERS if label == "fit" else NOT_FIT_TIERS})]))
    return cases


def load_suite(name: str) -> list[EvalCase]:
    if name == "golden":
        return _golden_cases()
    if name == "calibration":
        return _calibration_cases()
    if name not in SUITE_MODES:
        raise ValueError(f"unknown suite {name!r}; choose from {sorted(SUITE_MODES)}")
    return load_cases(DATASETS / name, DATASETS / "corpora")


def _norm_domain_icp(icp: str | None) -> Path:
    if icp is None:
        return DATASETS / DEFAULT_ICP
    p = Path(icp)
    return p if p.is_absolute() else DATASETS / icp


class LeadResearchTarget:
    def __init__(self, settings: Settings, timeout_s: float = 120.0):
        self.s = settings
        self.timeout_s = timeout_s
        self.model = settings.llm_model
        self._pool: psycopg_pool.AsyncConnectionPool | None = None
        self._llm_real: LangChainLLM | None = None
        self._tavily: TavilySearch | None = None

    # --- lifecycle -----------------------------------------------------------------------
    async def setup(self) -> None:
        self._pool = psycopg_pool.AsyncConnectionPool(
            self.s.database_url.get_secret_value(), open=False)
        await self._pool.open()

    async def teardown(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    # --- tenant lifecycle (admin role; app_user cannot create tenants) -------------------
    def _admin(self):
        if self.s.admin_database_url is None:
            raise RuntimeError("ADMIN_DATABASE_URL is required for evals")
        return psycopg.AsyncConnection.connect(
            self.s.admin_database_url.get_secret_value(), autocommit=True)

    async def _create_tenant(self, tenant: str) -> None:
        async with await self._admin() as c:
            await c.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                            (tenant, tenant))

    async def _purge(self, tenant: str) -> None:
        # FK-safe order; every statement is scoped by this run's own tenant id.
        async with await self._admin() as c:
            for table in ("tenant_usage", "lead_icp_evaluations", "leads", "icp_segments"):
                await c.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
            await c.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant,))

    async def _seed_icp(self, tenant: str, icp: str | None) -> None:
        for seg in json.loads(_norm_domain_icp(icp).read_text()):
            async with self._pool.connection() as conn, AsyncTenantSession(conn, tenant) as s:
                await IcpRepo(s).create_version(
                    seg["segment_key"], IcpSegmentIn.model_validate(seg["body"]))

    # --- dependency wiring ---------------------------------------------------------------
    def _deps(self, case: EvalCase, mode: str):
        if mode == "scripted":
            sc = case.scripted or {}
            llm = ScriptedLLM([*sc.get("llm", {}).get("plans", [])],
                              [*sc.get("llm", {}).get("synths", [])])
            cfg = ResearchConfig(**{"retry_backoff_seconds": 0.0, **sc.get("config", {})})
        else:
            if not self.s.llm_model:
                raise RuntimeError("LLM_MODEL is not set")
            self._llm_real = self._llm_real or LangChainLLM(self.s.llm_model)
            llm = self._llm_real
            cfg = ResearchConfig.from_settings(self.s)
        if mode == "live":
            if self.s.tavily_api_key is None:
                raise RuntimeError("TAVILY_API_KEY is not set (live mode)")
            self._tavily = self._tavily or TavilySearch(self.s.tavily_api_key.get_secret_value())
            search = self._tavily
        else:
            corpus = case.corpus or {"by_query": {}, "default": []}
            search = FixtureSearch(
                {q: [SearchResult(**r) for r in rs] for q, rs in corpus.get("by_query", {}).items()},
                [SearchResult(**r) for r in corpus.get("default", [])])
        return search, llm, cfg

    # --- one run -------------------------------------------------------------------------
    async def run_case(self, case: EvalCase, repeat: int, mode: str) -> CaseRun:
        tenant = f"eval-{uuid4().hex[:12]}"
        meter, t0 = UsageMeter(), time.monotonic()
        outcome, error = {}, None
        try:
            await self._create_tenant(tenant)
            await self._seed_icp(tenant, case.icp)
            search, llm, cfg = self._deps(case, mode)
            graph = build_graph(ResearchDeps(
                self._pool, MeteredSearch(search, meter), MeteredLLM(llm, meter), cfg))
            inp = case.input
            state = {"tenant_id": tenant, "lead_id": f"{case.id}-r{repeat}"[:80],
                     "job_id": str(uuid4()), "company_name": inp["company_name"],
                     "domain": inp.get("domain"), "trace": []}
            config = {"run_name": f"eval:{case.suite}:{case.id}", "tags": ["eval", case.suite],
                      "metadata": {"case_id": case.id, "repeat": repeat, "mode": mode,
                                   "suite": case.suite}}
            async with asyncio.timeout(self.timeout_s):
                final = await graph.ainvoke(state, config)
            outcome = self._outcome(final, cfg, search)
        except TimeoutError:
            error = f"timeout after {self.timeout_s}s"
        except Exception as exc:  # noqa: BLE001  (any failure becomes a recorded run error)
            error = redact(f"{type(exc).__name__}: {exc}")[:300]
        finally:
            await self._purge(tenant)  # always, including after errors and timeouts
        return CaseRun(case_id=case.id, repeat=repeat, outcome=outcome, error=error,
                       llm_calls=meter.llm_calls, searches=meter.searches,
                       duration_s=round(time.monotonic() - t0, 3))

    @staticmethod
    def _outcome(final: dict, cfg: ResearchConfig, search) -> dict:
        ev, profile = final["evaluation"], final["profile"]
        verdicts = [v for vs in ev.verdicts.values() for v in vs]
        quotes = sorted({q for v in verdicts for q in v.evidence_quotes})
        sources = final.get("sources", [])
        norm = [s.get("norm_text") or normalize_text(s["text"]) for s in sources]
        unverified = [q for q in quotes if not any(quote_in_text(q, n) for n in norm)]
        texts = [profile.industry, *profile.signals, *(v.rationale for v in verdicts)]
        canary = final.get("canary")
        return {
            "tier": ev.tier, "score": ev.score, "status": ev.status, "flags": sorted(ev.flags),
            "flags_detail": ev.flags_detail, "entity_confirmed": ev.entity_confirmed,
            "best_segment_key": ev.best_segment_key, "best_rank": ev.best_rank,
            "segments": {s.segment_key: {"score": s.score, "tier": s.tier} for s in ev.segments},
            "grades": {seg: {v.criterion_id: v.grade for v in vs}
                       for seg, vs in ev.verdicts.items()},
            "employee_count": profile.employee_count,
            "text_fields": [t for t in texts if t],
            "quotes": quotes, "unverified_quotes": unverified,
            "canary": getattr(canary, "token", None),
            "trace": final.get("trace", []),
            "queries": list(search.queries) if isinstance(search, FixtureSearch)
            else final.get("used_queries", []),
            "caps": {"max_llm_calls": cfg.max_llm_calls, "max_searches": cfg.max_searches},
            "_source_texts": [s["text"] for s in sources],
        }
