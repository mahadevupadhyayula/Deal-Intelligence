"""Cleaning of model-produced free text before it is verified, stored or displayed.

WHAT: `clean_findings` sanitises `industry`, `signals` and every judgement `rationale` and
detects canary (prompt) leaks.
WHERE: Called by the `validate` node on the raw `ResearchFindings`, before evidence checks.
WHY: These fields are unverified model text that gets stored, rendered in the UI and later
ingested by the wiki, so markup/links/hidden characters are stripped and a leaked system prompt
or canary is nulled. Evidence quotes are NOT touched here: they are verified verbatim against
(already sanitised) source text instead. Returns the cleaned findings plus whether a leak
was seen, so the caller can raise `prompt_leak_suspected`.
"""
from agents.guardrails.injection import CanaryGuard
from agents.guardrails.sanitize import sanitize_model_text
from agents.lead_research.models import ResearchFindings

MAX_INDUSTRY, MAX_SIGNAL, MAX_RATIONALE = 120, 200, 500


def clean_findings(findings: ResearchFindings, guard: CanaryGuard) -> tuple[ResearchFindings, bool]:
    leaked = False

    def clean(text: str | None, limit: int) -> str | None:
        nonlocal leaked
        if guard.leaked(text):
            leaked = True
            return None
        return sanitize_model_text(text, limit)

    judgements = {}
    for key, draft in findings.judgements.items():
        judgements[key] = draft.model_copy(update={"rationale": clean(draft.rationale, MAX_RATIONALE) or ""})
    signals = [c for c in (clean(s, MAX_SIGNAL) for s in findings.signals) if c]
    cleaned = findings.model_copy(update={
        "industry": clean(findings.industry, MAX_INDUSTRY), "signals": signals,
        "judgements": judgements})
    return cleaned, leaked
