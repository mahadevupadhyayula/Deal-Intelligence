"""Graph nodes for the Lead Research agent.

WHAT: `build_nodes(deps)` returns the node functions: load_icp, resolve_entity, plan_queries,
search, synthesize, validate, score, persist, plus the routing function.
WHERE: Wired into a LangGraph `StateGraph` by `graph.py`; run inside an ARQ worker.
WHY: Only plan_queries and synthesize call the LLM; everything that decides a result (evidence
verification, grading, scoring, refine decision) is deterministic code. Budgets (searches, LLM
calls, sources, characters) are checked before every external call, and exhausting any of them
degrades to `status="partial"` rather than looping or failing. Transient search/LLM errors are
retried within the same budget.
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agents.icp.scoring import evaluate_segments
from agents.lead_research.config import ResearchConfig
from agents.lead_research.llm import LLMError, StructuredLLM
from agents.lead_research.models import QueryPlan, ResearchFindings
from agents.lead_research.plan import build_plan
from agents.lead_research.profile import build_profile
from agents.lead_research.prompts import PLAN_SYSTEM, SYNTH_SYSTEM, plan_user, synth_user
from agents.lead_research.retry import call_with_retries
from agents.lead_research.state import ResearchState
from agents.lead_research.text import (
    host_of,
    normalize_domain,
    normalize_text,
    normalize_url,
    on_domain,
    sanitize_source_text,
)
from agents.lead_research.tools import SearchError, SearchResult, SearchTool
from agents.lead_research.validate import (
    VerifiedJudgement,
    VerifiedMetric,
    all_evidence,
    entity_confirmed,
    expand_verdicts,
    index_sources,
    validate_findings,
)
from agents.repos.async_session import AsyncTenantSession
from agents.repos.evaluation import AsyncEvaluationRepo
from agents.repos.icp import IcpRepo
from agents.repos.lead_async import AsyncLeadRepo
from agents.schemas import IcpEvaluation

# Directory/aggregator sites are never "the company's own site" when resolving a domain.
_NOT_OFFICIAL = {"linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
                 "youtube.com", "crunchbase.com", "wikipedia.org", "glassdoor.com", "g2.com",
                 "clutch.co", "yelp.com", "hubspot.com", "zoominfo.com", "bloomberg.com"}
_MAX_QUERIES = 4


class NoActiveIcpError(RuntimeError):
    """The tenant has no active ICP segment, so there is nothing to evaluate against."""


@dataclass
class ResearchDeps:
    pool: Any
    search: SearchTool
    llm: StructuredLLM
    cfg: ResearchConfig


def build_nodes(deps: ResearchDeps):
    cfg = deps.cfg

    async def _search(query: str, used: int) -> tuple[list[SearchResult], int, bool]:
        """One search with retries. Returns (results, calls_made, degraded)."""
        try:
            results, calls = await call_with_retries(
                lambda: deps.search.search(query, 5), attempts=cfg.retry_attempts,
                backoff=cfg.retry_backoff_seconds, max_calls=cfg.max_searches - used)
            return results, calls, False
        except SearchError as exc:
            if not exc.transient:
                raise  # bad key / bad request: fail the job loudly, do not hide it
            return [], getattr(exc, "calls_made", 1), True

    # ---- nodes ----------------------------------------------------------------------------

    async def load_icp(state: ResearchState):
        async with deps.pool.connection() as conn, AsyncTenantSession(
            conn, state["tenant_id"]
        ) as s:
            segments = await IcpRepo(s).list_active()
        if not segments:
            raise NoActiveIcpError("no active ICP defined")
        plan = build_plan(segments)
        return {
            "segments": segments, "plan": plan, "pending_keys": set(plan.judged),
            "pending_metrics": set(plan.metrics), "passes": 0, "searches_used": 0,
            "llm_calls_used": 0, "partial": False, "flags": [], "sources": [],
            "used_queries": [], "judgements": {}, "metrics": {}, "signals": [],
            "industry": None, "trace": ["load_icp"],
        }

    async def resolve_entity(state: ResearchState):
        if state.get("domain"):
            return {"resolved_domain": normalize_domain(state["domain"]),
                    "trace": ["resolve_entity"]}
        used = state["searches_used"]
        if used >= cfg.max_searches:
            return {"resolved_domain": None, "partial": True, "trace": ["resolve_entity"]}
        results, calls, degraded = await _search(f"{state['company_name']} official website", used)
        name = normalize_text(state["company_name"])
        resolved = None
        for r in results:
            host = host_of(r.url)
            if any(on_domain(host, bad) for bad in _NOT_OFFICIAL):
                continue
            if name in normalize_text(r.title + " " + r.content):
                resolved = host
                break
        flags = list(state["flags"]) + (["search_degraded"] if degraded else [])
        return {"resolved_domain": resolved, "searches_used": used + calls, "flags": flags,
                "trace": ["resolve_entity"]}

    async def plan_queries(state: ResearchState):
        passes = state["passes"] + 1
        used = state["llm_calls_used"]
        base = {"passes": passes, "trace": ["plan_queries"]}
        if used >= cfg.max_llm_calls:
            return {**base, "queries": [], "partial": True}
        user = plan_user(state["company_name"], state.get("resolved_domain"), state["plan"],
                         state["pending_keys"], state["pending_metrics"], state["used_queries"])
        try:
            plan, calls = await call_with_retries(
                lambda: deps.llm.generate(QueryPlan, PLAN_SYSTEM, user),
                attempts=cfg.retry_attempts, backoff=cfg.retry_backoff_seconds,
                max_calls=cfg.max_llm_calls - used)
        except LLMError as exc:
            if not exc.transient:
                raise
            return {**base, "queries": [], "partial": True,
                    "llm_calls_used": used + getattr(exc, "calls_made", 1),
                    "flags": [*state["flags"], "llm_degraded"]}
        seen = {q.casefold() for q in state["used_queries"]}
        queries = []
        for q in plan.queries:
            q = q.strip()
            if q and q.casefold() not in seen:
                seen.add(q.casefold())
                queries.append(q)
        return {**base, "queries": queries[:_MAX_QUERIES], "llm_calls_used": used + calls}

    async def search(state: ResearchState):
        used, partial, flags = state["searches_used"], state["partial"], list(state["flags"])
        by_norm = {s["norm_url"]: s for s in state["sources"]}
        used_queries = list(state["used_queries"])
        for q in state["queries"]:
            if used >= cfg.max_searches:
                partial = True
                break
            results, calls, degraded = await _search(q, used)
            used += calls
            used_queries.append(q)
            if degraded and "search_degraded" not in flags:
                flags.append("search_degraded")
                partial = True
            for r in results:
                norm = normalize_url(r.url)
                if norm in by_norm:
                    continue
                host = host_of(r.url)
                by_norm[norm] = {
                    "url": r.url, "norm_url": norm, "host": host, "title": r.title,
                    "text": sanitize_source_text(r.content)[: cfg.source_char_limit],
                    "on_domain": on_domain(host, state.get("resolved_domain")),
                }
        # Company's own pages first (stable otherwise), then cap count and total size.
        ranked = sorted(by_norm.values(), key=lambda s: not s["on_domain"])[: cfg.max_sources]
        kept, total = [], 0
        for s in ranked:
            room = cfg.max_total_chars - total
            if room < 200:
                break
            s = {**s, "text": s["text"][:room]}
            total += len(s["text"])
            kept.append(s)
        return {"sources": kept, "searches_used": used, "partial": partial, "flags": flags,
                "used_queries": used_queries, "trace": ["search"]}

    async def synthesize(state: ResearchState):
        base = {"findings": None, "trace": ["synthesize"]}
        if not state["sources"]:
            return base  # nothing to read: skip the LLM call entirely
        used = state["llm_calls_used"]
        if used >= cfg.max_llm_calls:
            return {**base, "partial": True}
        sources = [{**s, "id": f"s{i + 1}"} for i, s in enumerate(state["sources"])]
        user = synth_user(state["company_name"], state.get("resolved_domain"), state["plan"],
                          state["pending_keys"], state["pending_metrics"], sources)
        try:
            findings, calls = await call_with_retries(
                lambda: deps.llm.generate(ResearchFindings, SYNTH_SYSTEM, user),
                attempts=cfg.retry_attempts, backoff=cfg.retry_backoff_seconds,
                max_calls=cfg.max_llm_calls - used)
        except LLMError as exc:
            if not exc.transient:
                raise
            return {**base, "partial": True, "llm_calls_used": used + getattr(exc, "calls_made", 1),
                    "flags": [*state["flags"], "llm_degraded"]}
        return {"findings": findings, "llm_calls_used": used + calls, "trace": ["synthesize"]}

    async def validate(state: ResearchState):
        by_url = index_sources(state["sources"])
        findings: ResearchFindings | None = state.get("findings")
        judgements: dict[str, VerifiedJudgement] = state["judgements"]
        metrics: dict[str, VerifiedMetric] = state["metrics"]
        industry, signals = state["industry"], list(state["signals"])
        if findings is not None:
            judgements, metrics = validate_findings(
                findings, by_url, state["plan"], state["pending_keys"], state["pending_metrics"],
                judgements, metrics)
            industry = industry or (findings.industry or None)
            signals += [s for s in findings.signals if s not in signals]
        else:  # keep prior results, but make sure every key/metric has an entry
            judgements = {k: judgements.get(k, VerifiedJudgement()) for k in state["plan"].judged}
            metrics = {m: metrics.get(m, VerifiedMetric()) for m in state["plan"].metrics}
        verdicts = expand_verdicts(state["plan"], state["segments"], state["tenant_id"],
                                   judgements, metrics)
        confirmed = entity_confirmed(all_evidence(judgements, metrics), by_url,
                                     state.get("resolved_domain"), state["company_name"])
        return {
            "judgements": judgements, "metrics": metrics, "industry": industry,
            "signals": signals, "verdicts": verdicts, "entity_confirmed": confirmed,
            "pending_keys": {k for k, j in judgements.items() if j.grade == "unknown"},
            "pending_metrics": {m for m, v in metrics.items() if v.value is None},
            "trace": ["validate"],
        }

    async def score(state: ResearchState):
        segments, verdicts = state["segments"], state["verdicts"]
        result = evaluate_segments(segments, verdicts)
        flags: list[str] = []
        if not state["entity_confirmed"]:
            result = result.model_copy(update={"tier": "insufficient_data"})
            flags.append("entity_unconfirmed")
        for seg_score in result.segments:
            for cid in seg_score.disqualified_by:
                v = next(v for v in verdicts[seg_score.segment_key] if v.criterion_id == cid)
                if len(v.evidence_quotes) == 1 and "single_source_disqualifier" not in flags:
                    flags.append("single_source_disqualifier")
        # Refine only if finishing the unknowns could change the overall tier.
        pessimistic = evaluate_segments(segments, verdicts, assume_unknown="none").tier
        optimistic = evaluate_segments(segments, verdicts, assume_unknown="ideal").tier
        return {"result": result, "eval_flags": flags, "can_improve": pessimistic != optimistic,
                "trace": ["score"]}

    def route(state: ResearchState) -> str:
        budget_left = (state["searches_used"] < cfg.max_searches
                       and state["llm_calls_used"] + 2 <= cfg.max_llm_calls)
        if (state["can_improve"] and state["passes"] <= cfg.max_refine_loops
                and budget_left and not state["partial"]):
            return "plan_queries"
        return "persist"

    async def persist(state: ResearchState):
        tenant_id, result = state["tenant_id"], state["result"]
        evidence = all_evidence(state["judgements"], state["metrics"])
        profile = build_profile(
            tenant_id=tenant_id, lead_id=state["lead_id"], company_name=state["company_name"],
            resolved_domain=state.get("resolved_domain"), industry=state["industry"],
            signals=state["signals"], metrics=state["metrics"], evidence=evidence,
            entity_confirmed=state["entity_confirmed"])
        flags = sorted({*state["flags"], *state["eval_flags"]})
        evaluation = IcpEvaluation(
            tenant_id=tenant_id, evaluation_id=uuid.uuid4(), lead_id=state["lead_id"],
            job_id=uuid.UUID(state["job_id"]),
            icp_versions={s.segment_key: s.version for s in state["segments"]},
            best_segment_key=result.best_segment_key, best_rank=result.best_rank,
            score=result.score, tier=result.tier, segments=result.segments,
            verdicts=state["verdicts"], entity_confirmed=state["entity_confirmed"],
            resolved_domain=state.get("resolved_domain"), flags=flags,
            status="partial" if state["partial"] else "complete",
            created_at=datetime.now(UTC))
        # One transaction: profile and evaluation are saved together or not at all.
        async with deps.pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
            await AsyncLeadRepo(s).upsert(profile)
            await AsyncEvaluationRepo(s).create(evaluation)
        return {"profile": profile, "evaluation": evaluation, "trace": ["persist"]}

    return {"load_icp": load_icp, "resolve_entity": resolve_entity, "plan_queries": plan_queries,
            "search": search, "synthesize": synthesize, "validate": validate, "score": score,
            "persist": persist}, route

