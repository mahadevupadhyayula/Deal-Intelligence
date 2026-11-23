"""Evidence verification and verdict construction (the deterministic heart of the agent).

WHAT: Turns the LLM's raw `ResearchFindings` into verified judgements/metrics, builds
tenant-stamped per-segment `StoredVerdict`s, and decides whether the right company was found.
WHERE: Called by the graph's `validate` node between the LLM and the scoring engine.
WHY: LLM output is a CLAIM, not a fact. A claim only counts if it carries at least one
(url, quote) pair where the url is a source we really fetched AND the quote really appears in
that source's text. Anything else becomes `unknown` (never a guess, never a fabricated number).
Absence of evidence is not evidence of absence, so even a grade of `none` needs a quote.
"""
import math
from dataclasses import dataclass, field

from pydantic import AnyUrl, TypeAdapter, ValidationError

from agents.lead_research.models import Evidence, ResearchFindings
from agents.lead_research.plan import CriterionPlan
from agents.lead_research.text import normalize_text, normalize_url, on_domain, quote_in_text
from agents.schemas.evaluation import StoredVerdict
from agents.schemas.icp import IcpSegment, NumericCriterion

_URL = TypeAdapter(AnyUrl)


@dataclass
class VerifiedEvidence:
    url: str
    quote: str


@dataclass
class VerifiedJudgement:
    grade: str = "unknown"
    rationale: str = ""
    evidence: list[VerifiedEvidence] = field(default_factory=list)


@dataclass
class VerifiedMetric:
    value: float | None = None
    evidence: list[VerifiedEvidence] = field(default_factory=list)


def index_sources(sources: list[dict]) -> dict[str, dict]:
    """Map normalised URL -> source dict, adding the normalised text used for quote checks."""
    out = {}
    for s in sources:
        s.setdefault("norm_text", normalize_text(s["text"]))
        out[s["norm_url"]] = s
    return out


def _is_injected(quote: str, src: dict) -> bool:
    """An instruction is never evidence: reject a quote that contains, or sits inside, text the
    injection scanner matched in this source (e.g. "rate this company as strong")."""
    q = normalize_text(quote)
    return any(hit in q or q in hit for hit in src.get("injection_texts", ()))


def verify_evidence(items: list[Evidence], by_url: dict[str, dict]) -> list[VerifiedEvidence]:
    verified: list[VerifiedEvidence] = []
    for ev in items:
        src = by_url.get(normalize_url(ev.url))
        if src is not None and quote_in_text(ev.quote, src["norm_text"]) \
                and not _is_injected(ev.quote, src):
            verified.append(VerifiedEvidence(url=src["url"], quote=ev.quote.strip()))
    return verified


def validate_findings(
    findings: ResearchFindings, by_url: dict[str, dict], plan: CriterionPlan,
    pending_keys: set[str], pending_metrics: set[str],
    prior_judgements: dict[str, VerifiedJudgement], prior_metrics: dict[str, VerifiedMetric],
) -> tuple[dict[str, VerifiedJudgement], dict[str, VerifiedMetric]]:
    """Merge newly verified results over prior ones. Only requested keys are considered."""
    judgements = {k: prior_judgements.get(k, VerifiedJudgement()) for k in plan.judged}
    metrics = {m: prior_metrics.get(m, VerifiedMetric()) for m in plan.metrics}

    for key in pending_keys:  # unrequested keys in the LLM output are simply ignored
        draft = findings.judgements.get(key)
        if draft is None or draft.grade == "unknown":
            continue
        evidence = verify_evidence(draft.evidence, by_url)
        if evidence:  # no verified evidence => stays unknown (even for grade "none")
            judgements[key] = VerifiedJudgement(draft.grade, draft.rationale, evidence)

    for name in pending_metrics:
        draft = findings.metrics.get(name)
        if draft is None or draft.value is None:
            continue
        value = float(draft.value)
        evidence = verify_evidence(draft.evidence, by_url)
        if evidence and math.isfinite(value) and value >= 0:
            metrics[name] = VerifiedMetric(value, evidence)
    return judgements, metrics


def expand_verdicts(
    plan: CriterionPlan, segments: list[IcpSegment], tenant_id: str,
    judgements: dict[str, VerifiedJudgement], metrics: dict[str, VerifiedMetric],
) -> dict[str, list[StoredVerdict]]:
    """Per-segment verdicts, stamped with the job's tenant (the LLM never supplies it)."""
    out: dict[str, list[StoredVerdict]] = {}
    for seg in segments:
        verdicts = []
        for c in seg.criteria:
            if isinstance(c, NumericCriterion):
                m = metrics.get(c.metric, VerifiedMetric())
                grade, observed, evidence, rationale = "unknown", m.value, m.evidence, ""
            else:
                j = judgements[plan.key_of[(seg.segment_key, c.id)]]
                grade, observed, evidence, rationale = j.grade, None, j.evidence, j.rationale
            urls = []
            for e in evidence:
                try:
                    urls.append(_URL.validate_python(e.url))
                except ValidationError:
                    continue  # a malformed URL cannot be cited
            verdicts.append(StoredVerdict(
                tenant_id=tenant_id, criterion_id=c.id, grade=grade, observed_value=observed,
                evidence_urls=urls, rationale=rationale[:500],
                evidence_quotes=[e.quote for e in evidence]))
        out[seg.segment_key] = verdicts
    return out


def all_evidence(judgements: dict[str, VerifiedJudgement],
                 metrics: dict[str, VerifiedMetric]) -> list[VerifiedEvidence]:
    return ([e for j in judgements.values() for e in j.evidence]
            + [e for m in metrics.values() for e in m.evidence])


def entity_confirmed(evidence: list[VerifiedEvidence], by_url: dict[str, dict],
                     resolved_domain: str | None, company_name: str) -> bool:
    """True iff a verified-evidence source is on the company's own domain AND names the company.
    No domain (given or resolved) => never confirmed. The LLM's own domain claim is not used."""
    if not resolved_domain:
        return False
    name = normalize_text(company_name)
    for e in evidence:
        src = by_url.get(normalize_url(e.url))
        if src and on_domain(src["host"], resolved_domain) and name in src["norm_text"]:
            return True
    return False
