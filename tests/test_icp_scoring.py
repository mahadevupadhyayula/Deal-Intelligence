"""Gate 1 tests for the deterministic scoring engine (pure: no DB, no network).

WHAT: Exact-number scoring, numeric grading, unknown/disqualifier handling, multi-segment
selection.
WHERE: Verifies `agents/icp/scoring.py`.
WHY: Scores drive sales decisions; every rule is pinned by a test with hand-computed numbers.
"""
import pytest

from agents.icp.scoring import evaluate_segments, score_segment
from agents.schemas.icp import IcpSegment
from tests.icp_helpers import example, judged, numeric, segment, verdict


def seg(*criteria, **kw):
    return segment(list(criteria), **kw)


def test_all_ideal_is_100_and_strong():
    s = seg(judged("a", 5), judged("b", 5))
    r = score_segment(s, [verdict("a", "ideal"), verdict("b", "ideal")])
    assert (r.score, r.tier, r.evidence_ratio) == (100.0, "strong", 1.0)


def test_all_none_is_zero_not_fit():
    s = seg(judged("a"), judged("b"))
    r = score_segment(s, [verdict("a", "none"), verdict("b", "none")])
    assert (r.score, r.tier) == (0.0, "not_fit")


@pytest.mark.parametrize("grade,points", [("ideal", 100.0), ("acceptable", 75.0),
                                          ("secondary", 50.0), ("none", 0.0)])
def test_grade_points(grade, points):
    r = score_segment(seg(judged("a")), [verdict("a", grade)])
    assert r.score == points


def test_weights_matter():
    s = seg(judged("heavy", 9), judged("light", 1))
    r1 = score_segment(s, [verdict("heavy", "ideal"), verdict("light", "none")])
    r2 = score_segment(s, [verdict("heavy", "none"), verdict("light", "ideal")])
    assert (r1.score, r2.score) == (90.0, 10.0)


def test_mixed_exact_number_and_contributions_sum_to_score():
    s = seg(judged("a", 6), judged("b", 4))  # 6*0.75 + 4*0.5 = 6.5 of 10
    r = score_segment(s, [verdict("a", "acceptable"), verdict("b", "secondary")])
    assert r.score == 65.0 and r.contributions == {"a": 45.0, "b": 20.0}
    assert sum(r.contributions.values()) == pytest.approx(r.score)


@pytest.mark.parametrize("score_grade,tier", [("ideal", "strong"), ("acceptable", "moderate"),
                                              ("secondary", "moderate"), ("none", "not_fit")])
def test_thresholds_inclusive(score_grade, tier):
    # 100 -> strong (>=75); 75 -> strong boundary handled below; 50 -> moderate (>=50)
    r = score_segment(seg(judged("a")), [verdict("a", score_grade)])
    expected = {"ideal": "strong", "acceptable": "strong", "secondary": "moderate",
                "none": "not_fit"}[score_grade]
    assert r.tier == expected


def test_weak_and_boundaries():
    s = seg(judged("a", 4), judged("b", 6), thresholds={"strong": 75, "moderate": 50, "weak": 25})
    # a none, b secondary: 6*0.5/10 = 30 -> weak (>=25)
    assert score_segment(s, [verdict("a", "none"), verdict("b", "secondary")]).tier == "weak"
    # a ideal, b none: 40 -> weak ; a none b acceptable: 45 -> weak ; ensure 25 exactly:
    s2 = seg(judged("a", 1), judged("b", 3), thresholds={"strong": 75, "moderate": 50, "weak": 25})
    r = score_segment(s2, [verdict("a", "ideal"), verdict("b", "none")])
    assert r.score == 25.0 and r.tier == "weak"


@pytest.mark.parametrize("value,grade_score", [(12, 100.0), (25, 75.0), (40, 50.0), (60, 0.0),
                                               (8, 100.0), (20, 100.0), (30, 75.0), (31, 50.0),
                                               (50, 50.0), (51, 0.0), (4, 0.0), ("12", 100.0)])
def test_numeric_graded_in_code(value, grade_score):
    r = score_segment(seg(numeric()), [verdict("size", observed_value=value)])
    assert r.score == grade_score


@pytest.mark.parametrize("value", [None, "about twenty", ""])
def test_numeric_unparseable_is_unknown(value):
    r = score_segment(seg(numeric(), judged("a")), [verdict("size", observed_value=value),
                                                    verdict("a", "ideal")])
    assert r.evidence_ratio == 0.5 and r.contributions.keys() == {"a"}


def test_llm_grade_ignored_for_numeric():
    r = score_segment(seg(numeric()), [verdict("size", "ideal", observed_value=99)])
    assert r.score == 0.0  # 99 is outside every band, whatever the LLM claimed


def test_open_ended_band():
    s = seg(numeric(bands=[{"grade": "ideal", "min": 100}]))
    assert score_segment(s, [verdict("size", observed_value=5000)]).score == 100.0


def test_unknown_excluded_from_denominator():
    s = seg(judged("a", 5), judged("b", 5))
    r = score_segment(s, [verdict("a", "ideal"), verdict("b", "unknown")])
    assert (r.score, r.evidence_ratio, r.tier) == (100.0, 0.5, "strong")  # floor is inclusive


def test_insufficient_data_below_floor_still_reports_score():
    s = seg(judged("a", 2), judged("b", 8))
    r = score_segment(s, [verdict("a", "ideal")])  # known weight 2 of 10 = 0.2 < 0.5
    assert (r.tier, r.score, r.evidence_ratio) == ("insufficient_data", 100.0, 0.2)


def test_all_unknown_is_insufficient_with_zero_score():
    r = score_segment(seg(judged("a")), [])
    assert (r.tier, r.score, r.evidence_ratio) == ("insufficient_data", 0.0, 0.0)


def test_disqualifier_triggers_not_fit():
    s = seg(judged("a"), judged("dq", role="disqualifier"))
    r = score_segment(s, [verdict("a", "ideal"), verdict("dq", "ideal")])
    assert (r.tier, r.score, r.disqualified_by) == ("not_fit", 0.0, ["dq"])


def test_disqualifier_none_has_no_effect_and_unknown_is_listed():
    s = seg(judged("a"), judged("dq", role="disqualifier"), judged("dq2", role="disqualifier"))
    r = score_segment(s, [verdict("a", "ideal"), verdict("dq", "none")])
    assert r.tier == "strong" and r.disqualified_by == [] and r.unverified_disqualifiers == ["dq2"]


def test_disqualifier_beats_insufficient_data():
    s = seg(judged("a"), judged("dq", role="disqualifier"))
    r = score_segment(s, [verdict("dq", "acceptable")])
    assert r.tier == "not_fit"


def test_invalid_verdicts_rejected():
    s = seg(judged("a"))
    with pytest.raises(ValueError, match="unknown criterion"):
        score_segment(s, [verdict("zzz", "ideal")])
    with pytest.raises(ValueError, match="duplicate"):
        score_segment(s, [verdict("a", "ideal"), verdict("a", "none")])
    with pytest.raises(ValueError, match="tenant"):
        score_segment(s, [verdict("a", "ideal", tenant="other")])


def test_deterministic():
    s = seg(judged("a", 3), judged("b", 7), numeric())
    v = [verdict("a", "acceptable"), verdict("b", "ideal"), verdict("size", observed_value=15)]
    assert score_segment(s, v) == score_segment(s, list(reversed(v)))


# ---- multi-segment -------------------------------------------------------------------------

def two(primary_grade, secondary_grade):
    p = segment([judged("a")], key="primary-seg", rank="primary")
    sec = segment([judged("a")], key="secondary-seg", rank="secondary")
    return [p, sec], {"primary-seg": [verdict("a", primary_grade)],
                      "secondary-seg": [verdict("a", secondary_grade)]}


def test_best_segment_by_tier_and_secondary_reported_as_secondary():
    segs, v = two("none", "ideal")
    r = evaluate_segments(segs, v)
    assert (r.best_segment_key, r.best_rank, r.tier, r.score) == ("secondary-seg", "secondary",
                                                                  "strong", 100.0)


def test_tie_prefers_primary():
    segs, v = two("ideal", "ideal")
    assert evaluate_segments(segs, v).best_rank == "primary"


def test_higher_score_wins_within_same_tier():
    segs, v = two("ideal", "acceptable")  # both strong (100 vs 75)
    assert evaluate_segments(segs, v).best_segment_key == "primary-seg"
    segs, v = two("acceptable", "ideal")
    assert evaluate_segments(segs, v).best_segment_key == "secondary-seg"


def test_insufficient_segment_blocks_weak_verdict():
    p = segment([judged("a")], key="primary-seg")
    sec = segment([judged("a")], key="secondary-seg", rank="secondary")
    r = evaluate_segments([p, sec], {"primary-seg": [verdict("a", "secondary")]})  # primary weak?
    # primary: score 50 -> moderate (>=50) so use none instead for a genuinely weak/not_fit case
    r = evaluate_segments([p, sec], {"primary-seg": [verdict("a", "none")]})
    assert r.tier == "insufficient_data" and r.best_segment_key == "secondary-seg"


def test_strong_beats_insufficient_elsewhere():
    p = segment([judged("a")], key="primary-seg")
    sec = segment([judged("a")], key="secondary-seg", rank="secondary")
    r = evaluate_segments([p, sec], {"secondary-seg": [verdict("a", "ideal")]})
    assert r.tier == "strong" and r.best_segment_key == "secondary-seg"


def test_all_not_fit():
    segs, v = two("none", "none")
    assert evaluate_segments(segs, v).tier == "not_fit"


def test_evaluate_input_validation():
    with pytest.raises(ValueError):
        evaluate_segments([], {})
    segs, _ = two("ideal", "ideal")
    with pytest.raises(ValueError, match="unknown segment"):
        evaluate_segments(segs, {"nope": []})


def test_example_segments_score_end_to_end():
    from agents.schemas.icp import IcpSegmentIn
    body = example("primary")
    IcpSegmentIn.model_validate(body)
    seg = IcpSegment.model_validate({**body, "tenant_id": "t1", "segment_key": "p", "version": 1})
    verdicts = [verdict(c.id, "ideal") for c in seg.criteria if c.kind == "judged"]
    verdicts.append(verdict("company-size", observed_value=12))
    r = score_segment(seg, verdicts)
    assert (r.score, r.tier) == (100.0, "strong")
    # 51 employees is outside every band of the primary: only the size criterion changes
    verdicts[-1] = verdict("company-size", observed_value=51)
    assert score_segment(seg, verdicts).score < 100.0
