"""Gate 1 tests for ICP model validation.

WHAT: Rejects malformed segments; accepts the shipped examples.
WHERE: Verifies `agents/schemas/icp.py`.
WHY: ICPs are tenant-authored input; bad definitions must fail at the boundary with clear
errors rather than corrupt scoring later.
"""
import pytest
from pydantic import ValidationError

from agents.schemas.icp import IcpSegmentIn
from tests.icp_helpers import body, example, judged, numeric


def bad(criteria=None, **kw):
    with pytest.raises(ValidationError):
        IcpSegmentIn.model_validate(body([judged("a")] if criteria is None else criteria, **kw))


def test_valid_minimal():
    IcpSegmentIn.model_validate(body([judged("a")]))


def test_duplicate_ids_rejected():
    bad([judged("a"), judged("a")])


def test_needs_a_scoring_criterion():
    bad([judged("a", role="disqualifier")])


@pytest.mark.parametrize("w", [0, 11, -1])
def test_weight_out_of_range(w):
    bad([judged("a", weight=w)])


def test_scoring_requires_weight():
    c = judged("a")
    del c["weight"]
    bad([c])


def test_disqualifier_must_not_have_weight():
    c = judged("dq", role="disqualifier")
    c["weight"] = 3
    bad([judged("a"), c])


@pytest.mark.parametrize("band", [
    {"grade": "ideal"},                      # boundless
    {"grade": "ideal", "min": 10, "max": 5},  # min > max
    {"grade": "none", "min": 1, "max": 2},   # not a band grade
])
def test_bad_bands(band):
    bad([numeric(bands=[band])])


def test_overlapping_bands_are_allowed():
    IcpSegmentIn.model_validate(body([numeric()]))  # 8-20 inside 5-30 by design


@pytest.mark.parametrize("th", [
    {"strong": 50, "moderate": 50, "weak": 25},
    {"strong": 30, "moderate": 50, "weak": 25},
    {"strong": 101, "moderate": 50, "weak": 25},
    {"strong": 75, "moderate": 50, "weak": 0},
])
def test_bad_thresholds(th):
    bad(thresholds=th)


def test_unknown_fields_and_server_fields_rejected():
    bad(tenant_id="evil")
    bad(version=3)
    bad(bogus=1)
    c = judged("a")
    c["surprise"] = 1
    bad([c])


@pytest.mark.parametrize("kw", [{"rank": "tertiary"}, {"min_evidence_ratio": 1.5}, {"name": ""}])
def test_bad_top_level_values(kw):
    with pytest.raises(ValidationError):
        IcpSegmentIn.model_validate({**body([judged("a")]), **kw})


def test_bad_criteria_count():
    bad([])
    bad([judged(f"c{i}") for i in range(26)])


def test_bad_persona():
    bad(personas=[{"tier": 3, "label": "x", "roles": ["a"]}])
    bad(personas=[{"tier": 1, "label": "x", "roles": []}])


@pytest.mark.parametrize("name", ["primary", "secondary"])
def test_examples_validate(name):
    seg = IcpSegmentIn.model_validate(example(name))
    assert seg.rank == name and len(seg.personas) == 2


def test_boolean_observed_value_rejected():
    from agents.schemas.icp import CriterionVerdict
    with pytest.raises(ValidationError):
        CriterionVerdict(tenant_id="t1", criterion_id="size", observed_value=True)
