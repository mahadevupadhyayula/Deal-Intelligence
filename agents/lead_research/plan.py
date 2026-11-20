"""Criterion plan: which distinct things the LLM must judge.

WHAT: Deduplicates identical judged criteria across ICP segments into neutral keys (c1, c2...)
and collects the numeric metrics to extract.
WHERE: Built by the `load_icp` node; used by prompts, validation and verdict expansion.
WHY: (1) Segments often share criteria (e.g. company type); judging each once is cheaper and
guarantees the SAME attribute gets the SAME grade in every segment. (2) The LLM only ever sees
neutral keys, never tenant ids or segment keys, so it cannot influence them. Role is part of
the signature because "scoring" and "disqualifier" use the same rubric text differently.
"""
import json
from dataclasses import dataclass, field

from agents.schemas.icp import IcpSegment, NumericCriterion


@dataclass
class JudgedItem:
    key: str
    role: str
    rubric: str
    grade_hints: dict[str, str]
    non_match_examples: list[str]
    usage: list[tuple[str, str]] = field(default_factory=list)  # (segment_key, criterion_id)


@dataclass
class CriterionPlan:
    judged: dict[str, JudgedItem]
    metrics: list[str]
    key_of: dict[tuple[str, str], str]  # (segment_key, criterion_id) -> judged key


def build_plan(segments: list[IcpSegment]) -> CriterionPlan:
    by_signature: dict[str, JudgedItem] = {}
    key_of: dict[tuple[str, str], str] = {}
    metrics: list[str] = []
    for seg in sorted(segments, key=lambda s: s.segment_key):
        for c in sorted(seg.criteria, key=lambda c: c.id):
            if isinstance(c, NumericCriterion):
                if c.metric not in metrics:
                    metrics.append(c.metric)
                continue
            hints = c.grade_hints.model_dump(exclude_none=True) if c.grade_hints else {}
            sig = json.dumps([c.role, c.rubric, hints, c.non_match_examples], sort_keys=True)
            item = by_signature.get(sig)
            if item is None:
                item = JudgedItem(f"c{len(by_signature) + 1}", c.role, c.rubric, hints,
                                  list(c.non_match_examples))
                by_signature[sig] = item
            item.usage.append((seg.segment_key, c.id))
            key_of[(seg.segment_key, c.id)] = item.key
    return CriterionPlan({i.key: i for i in by_signature.values()}, metrics, key_of)
