"""Deterministic ICP scoring.

WHAT: `score_segment` (one segment) and `evaluate_segments` (all of a tenant's segments,
best match wins) turn criterion verdicts into a score, a tier and an audit trail.
WHERE: Called by the Lead Research agent's `score` node (Spec 005); can be re-run on stored
verdicts to verify a stored result.
WHY: The LLM only gathers evidence and judges rubrics; the NUMBER and TIER come from here,
so they are explainable, reproducible and immune to a model "deciding" a score. Design
trade-offs:
  * `unknown` is excluded from the denominator (missing evidence is not a bad fit), but if too
    little weight is known the tier becomes `insufficient_data` instead of a guess.
  * Numeric criteria are graded here from `observed_value`; any LLM-supplied grade is ignored.
  * A triggered disqualifier forces `not_fit` regardless of score.
"""
from typing import Literal

from agents.schemas.icp import (
    GRADE_POINTS,
    CriterionVerdict,
    IcpResult,
    IcpSegment,
    NumericCriterion,
    SegmentScore,
    Tier,
)

_TIER_ORDER: dict[str, int] = {"strong": 3, "moderate": 2, "weak": 1, "not_fit": 0}
_BAND_ORDER = {"ideal": 3, "acceptable": 2, "secondary": 1}


def _numeric_grade(criterion: NumericCriterion, observed) -> str:
    """Grade a number against overlapping bands; the best matching band wins."""
    if observed is None or isinstance(observed, bool):
        return "unknown"
    try:
        value = float(observed)
    except (TypeError, ValueError):
        return "unknown"  # e.g. "about 20" cannot be graded; do not guess
    best: str | None = None
    for band in criterion.bands:
        in_band = (band.min is None or value >= band.min) and (band.max is None or value <= band.max)
        if in_band and (best is None or _BAND_ORDER[band.grade] > _BAND_ORDER[best]):
            best = band.grade
    return best or "none"


AssumeUnknown = Literal["none", "ideal"] | None


def score_segment(
    segment: IcpSegment, verdicts: list[CriterionVerdict], assume_unknown: AssumeUnknown = None
) -> SegmentScore:
    by_id = {c.id: c for c in segment.criteria}
    seen: dict[str, CriterionVerdict] = {}
    for v in verdicts:
        if v.criterion_id not in by_id:
            raise ValueError(f"verdict for unknown criterion '{v.criterion_id}'")
        if v.criterion_id in seen:
            raise ValueError(f"duplicate verdict for criterion '{v.criterion_id}'")
        if v.tenant_id != segment.tenant_id:
            raise ValueError("verdict tenant_id does not match segment tenant_id")
        seen[v.criterion_id] = v

    def grade_of(c) -> str:
        v = seen.get(c.id)  # a missing verdict counts as unknown
        if v is None:
            return "unknown"
        if isinstance(c, NumericCriterion):
            return _numeric_grade(c, v.observed_value)
        return v.grade

    total_weight = known_weight = weighted_points = 0.0
    points_by_id: dict[str, tuple[int, float]] = {}
    disqualified, unverified = [], []
    for c in segment.criteria:
        grade = grade_of(c)
        # Bounds mode (Spec 005): pretend unknown SCORING criteria are `none`/`ideal` to see
        # how far the tier could move. Unknown disqualifiers stay unknown (never triggered).
        if assume_unknown and grade == "unknown" and c.role == "scoring":
            grade = assume_unknown
        if c.role == "disqualifier":
            if grade == "unknown":
                unverified.append(c.id)
            elif grade != "none":
                disqualified.append(c.id)
            continue
        total_weight += c.weight
        if grade != "unknown":
            known_weight += c.weight
            weighted_points += c.weight * GRADE_POINTS[grade]
            points_by_id[c.id] = (c.weight, GRADE_POINTS[grade])

    evidence_ratio = known_weight / total_weight
    raw = 100 * weighted_points / known_weight if known_weight else 0.0
    # Round first, then compare: the reported number and the tier can never disagree.
    score = round(raw, 2)
    contributions = {
        cid: round(100 * w * p / known_weight, 2) for cid, (w, p) in points_by_id.items()
    }

    t = segment.thresholds
    tier: Tier
    if disqualified:
        tier, score, contributions = "not_fit", 0.0, {}
    elif evidence_ratio < segment.min_evidence_ratio:
        tier = "insufficient_data"
    elif score >= t.strong:
        tier = "strong"
    elif score >= t.moderate:
        tier = "moderate"
    elif score >= t.weak:
        tier = "weak"
    else:
        tier = "not_fit"

    return SegmentScore(
        tenant_id=segment.tenant_id, segment_key=segment.segment_key, version=segment.version,
        rank=segment.rank, score=score, tier=tier, evidence_ratio=round(evidence_ratio, 4),
        contributions=contributions, disqualified_by=disqualified,
        unverified_disqualifiers=unverified,
    )


def evaluate_segments(
    segments: list[IcpSegment],
    verdicts_by_segment: dict[str, list[CriterionVerdict]],
    assume_unknown: AssumeUnknown = None,
) -> IcpResult:
    if not segments:
        raise ValueError("at least one segment is required")
    keys = {s.segment_key for s in segments}
    stray = set(verdicts_by_segment) - keys
    if stray:
        raise ValueError(f"verdicts for unknown segment(s): {sorted(stray)}")

    # Sort by key first: `max` keeps the first of equal candidates, so ties are deterministic.
    scores = [
        score_segment(s, verdicts_by_segment.get(s.segment_key, []), assume_unknown)
        for s in sorted(segments, key=lambda s: s.segment_key)
    ]

    def rank_key(s: SegmentScore):  # higher is better; primary beats secondary on a tie
        return (_TIER_ORDER.get(s.tier, -1), s.score, 1 if s.rank == "primary" else 0)

    scored = [s for s in scores if s.tier != "insufficient_data"]
    insufficient = [s for s in scores if s.tier == "insufficient_data"]
    best_scored = max(scored, key=rank_key) if scored else None

    if best_scored and best_scored.tier in ("strong", "moderate"):
        best = best_scored
    elif insufficient:
        # A better match may be hiding in the unknowns, so refuse to call it weak/not_fit.
        best = max(insufficient, key=lambda s: (s.score, 1 if s.rank == "primary" else 0))
    else:
        best = best_scored

    return IcpResult(
        tenant_id=segments[0].tenant_id, segments=scores, best_segment_key=best.segment_key,
        best_rank=best.rank, tier=best.tier, score=best.score,
    )
