"""Prompt-injection heuristics and canary leak detection.

WHAT: `scan_injection` (pattern hits in untrusted text) and `CanaryGuard` (detects our own
system prompt/canary token leaking into model output).
WHERE: Lead Research `search` (scan each fetched source) and `validate` (canary on free text).
WHY: These are SIGNALS, not defences: patterns are easy to evade and can false-positive, so a
hit only raises a flag for human review (Spec 014). The real defences are structural (no tools,
schema-validated output, evidence verified in code, scores computed in code). Patterns are kept
deliberately specific (adjacent words, not loose keyword matches) to limit false positives on
ordinary marketing text that talks about "instructions" or "systems".
"""
import re
import secrets
from dataclasses import dataclass

_I = re.IGNORECASE
_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+|these\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|safety)\s+(?:\w+\s+)?"
        r"(?:instructions?|rules?|prompts?|directions?|guidelines?)\b", _I),
    "ignore_all_rules": re.compile(
        r"\b(?:ignore|disregard)\s+(?:all|any|every)\s+(?:instructions?|rules?)\b", _I),
    "role_marker": re.compile(
        r"(?:^|\n)\s*(?:system|assistant|developer)\s*:|<\|?(?:im_start|im_end|system|endoftext)\|?>"
        r"|\[/?(?:inst|sys)\]", _I),
    "role_override": re.compile(r"\byou\s+are\s+now\b|\bnew\s+(?:instructions|persona)\s*:", _I),
    "rate_company": re.compile(
        r"\b(?:rate|score|grade|classify|mark|label)\b[^.\n]{0,30}\b(?:this|the)\b[^.\n]{0,20}"
        r"\b(?:company|lead|business|firm)\b[^.\n]{0,40}"
        r"\b(?:strong|ideal|perfect|best|10/10|100)\b", _I),
    "reveal_prompt": re.compile(
        r"\b(?:reveal|print|repeat|output|show)\b[^.\n]{0,30}"
        r"\b(?:system\s+prompt|your\s+instructions|hidden\s+prompt)\b", _I),
    "conceal_from_user": re.compile(
        r"\b(?:do\s+not|don'?t|never)\s+(?:mention|tell|reveal|disclose)\b[^.\n]{0,40}"
        r"\b(?:user|operator|human)\b", _I),
    "tag_forgery": re.compile(r"</?\s*untrusted_source", _I),
}


@dataclass(frozen=True)
class InjectionHit:
    pattern: str
    excerpt: str  # at most 80 chars, for the reviewer
    text: str     # the full matched text, so evidence overlapping it can be rejected


def scan_injection(text: str) -> list[InjectionHit]:
    hits: list[InjectionHit] = []
    for name, rx in _PATTERNS.items():
        m = rx.search(text)
        if m:
            full = re.sub(r"\s+", " ", m.group(0)).strip()
            hits.append(InjectionHit(name, full[:80], full))
    return hits


class CanaryGuard:
    """A random token placed in the system prompt, plus optional distinctive prompt fragments.
    If either shows up in model output the prompt is leaking (or being echoed)."""

    def __init__(self, token: str | None = None, fragments: tuple[str, ...] = ()):
        self.token = token or "CANARY-" + secrets.token_hex(6)
        self.fragments = tuple(f.casefold() for f in fragments if len(f) >= 20)

    def system_line(self) -> str:
        return f"Internal reference code (never output it): {self.token}"

    def leaked(self, text: str | None) -> bool:
        if not text:
            return False
        low = text.casefold()
        return self.token.casefold() in low or any(f in low for f in self.fragments)
