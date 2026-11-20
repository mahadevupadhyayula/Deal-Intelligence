"""Build the LeadProfile from verified research results.

WHAT: `build_profile` assembles a `LeadProfile` (Spec 001 model) and its `confidence`.
WHERE: Called by the graph's `persist` node.
WHY: `confidence` measures how well-sourced and complete the PROFILE data is (verified source
count, filled fields, entity confirmed). It is computed, never LLM-reported, and is a different
question from ICP fit (see IcpEvaluation). `industry` and `signals` are informational LLM text
(no quote requirement) so they are length-capped; employee_count comes only from the verified
metric.
"""
from pydantic import AnyUrl, TypeAdapter, ValidationError

from agents.lead_research.validate import VerifiedEvidence, VerifiedMetric
from agents.schemas.lead import LeadProfile

_URL = TypeAdapter(AnyUrl)


def profile_confidence(n_sources: int, filled_fields: int, entity_confirmed: bool) -> float:
    c = 0.4 * min(n_sources, 3) / 3 + 0.4 * filled_fields / 4 + (0.2 if entity_confirmed else 0.0)
    return round(max(0.0, min(1.0, c)), 4)


def build_profile(
    *, tenant_id: str, lead_id: str, company_name: str, resolved_domain: str | None,
    industry: str | None, signals: list[str], metrics: dict[str, VerifiedMetric],
    evidence: list[VerifiedEvidence], entity_confirmed: bool,
) -> LeadProfile:
    emp = metrics.get("employee_count")
    employee_count = round(emp.value) if emp and emp.value is not None else None
    industry = (industry or "").strip()[:200] or None
    signals = [s.strip()[:200] for s in signals if s and s.strip()][:10]

    urls: list[AnyUrl] = []
    seen: set[str] = set()
    for e in evidence:
        if e.url not in seen:
            seen.add(e.url)
            try:
                urls.append(_URL.validate_python(e.url))
            except ValidationError:
                continue
    filled = sum([bool(industry), employee_count is not None, bool(resolved_domain), bool(signals)])
    return LeadProfile(
        tenant_id=tenant_id, lead_id=lead_id, company_name=company_name, domain=resolved_domain,
        industry=industry, employee_count=employee_count, signals=signals, sources=urls,
        confidence=profile_confidence(len(urls), filled, entity_confirmed),
    )
