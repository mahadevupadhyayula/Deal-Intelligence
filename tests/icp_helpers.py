"""Shared builders for ICP tests.

WHAT: Small factory functions for segments, criteria and verdicts.
WHERE: Imported by the ICP model/scoring/repo/API tests.
WHY: Keeps each test focused on the behaviour it checks rather than on boilerplate.
"""
import json
from pathlib import Path

from agents.schemas.icp import CriterionVerdict, IcpSegment, IcpSegmentIn

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples" / "icp"


def judged(id, weight=5, role="scoring"):
    c = {"id": id, "kind": "judged", "label": id, "role": role, "rubric": f"rubric for {id}"}
    if role == "scoring":
        c["weight"] = weight
    return c


def numeric(id="size", weight=5, bands=None, role="scoring"):
    bands = bands or [{"grade": "ideal", "min": 8, "max": 20},
                      {"grade": "acceptable", "min": 5, "max": 30},
                      {"grade": "secondary", "min": 31, "max": 50}]
    c = {"id": id, "kind": "numeric", "label": id, "role": role, "metric": "employee_count",
         "bands": bands}
    if role == "scoring":
        c["weight"] = weight
    return c


def body(criteria, rank="primary", **kw):
    return {"name": "Test ICP", "rank": rank, "criteria": criteria, **kw}


def segment_in(criteria, rank="primary", **kw):
    return IcpSegmentIn.model_validate(body(criteria, rank, **kw))


def segment(criteria, tenant="t1", key="primary-seg", version=1, rank="primary", **kw):
    return IcpSegment.model_validate({**body(criteria, rank, **kw), "tenant_id": tenant,
                                      "segment_key": key, "version": version})


def verdict(criterion_id, grade="unknown", tenant="t1", **kw):
    return CriterionVerdict(tenant_id=tenant, criterion_id=criterion_id, grade=grade, **kw)


def example(name):
    return json.loads((EXAMPLES / f"{name}_hubspot_consultancy.json").read_text())
