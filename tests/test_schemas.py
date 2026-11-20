"""Gate 1 unit tests for the core Pydantic models.

WHAT: Validates tenant_id enforcement, field constraints, cross-tenant nesting
rejection, and JSON round-trips.
WHERE: Verifies `agents/schemas/`.
WHY: These contracts are the trust boundary for LLM output; they must fail loudly.
"""
import pytest
from pydantic import ValidationError

from agents.schemas import ConflictItem, DealState, HandoffBrief, LeadProfile


def lead(t="t1", **kw):
    return LeadProfile(tenant_id=t, lead_id="l1", company_name="Acme", confidence=0.5, **kw)


def conflict(t="t1", **kw):
    return ConflictItem(tenant_id=t, conflict_id="c1", deal_id="d1", field="amount", risk="low", **kw)


def test_missing_tenant_id_rejected():
    with pytest.raises(ValidationError):
        LeadProfile(lead_id="l1", company_name="Acme", confidence=0.5)


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_tenant_id_rejected(bad):
    with pytest.raises(ValidationError):
        lead(t=bad)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        lead(bogus=1)


@pytest.mark.parametrize("c", [-0.1, 1.1])
def test_confidence_range(c):
    with pytest.raises(ValidationError):
        LeadProfile(tenant_id="t1", lead_id="l", company_name="A", confidence=c)


def test_invalid_risk_and_status():
    with pytest.raises(ValidationError):
        ConflictItem(tenant_id="t1", conflict_id="c", deal_id="d", field="f", risk="medium")
    with pytest.raises(ValidationError):
        conflict(status="done")


def test_deal_rejects_cross_tenant_lead():
    with pytest.raises(ValidationError, match="tenant_id mismatch"):
        DealState(tenant_id="t1", deal_id="d1", stage="new", lead=lead(t="t2"))


def test_deal_rejects_cross_tenant_conflict():
    with pytest.raises(ValidationError, match="tenant_id mismatch"):
        DealState(tenant_id="t1", deal_id="d1", stage="new", conflicts=[conflict(t="t2")])


def test_deal_accepts_same_tenant_children():
    d = DealState(tenant_id="t1", deal_id="d1", stage="new", lead=lead(), conflicts=[conflict()])
    assert d.lead.tenant_id == "t1"


def test_json_round_trip():
    d = DealState(
        tenant_id="t1", deal_id="d1", stage="new", amount=10,
        lead=lead(sources=["https://example.com/a"]), conflicts=[conflict()],
    )
    assert DealState.model_validate_json(d.model_dump_json()) == d
    h = HandoffBrief(tenant_id="t1", deal_id="d1", summary="s")
    assert HandoffBrief.model_validate_json(h.model_dump_json()) == h
