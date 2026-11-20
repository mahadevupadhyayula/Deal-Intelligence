"""Gate 1 tests for TenantSession and the repositories (run as non-superuser app_user).

WHAT: Round-trips, cross-tenant isolation, RLS fail-closed/backstop, composite FKs.
WHERE: Verifies `agents/repos/` and migrations 0001/0002 together.
WHY: Tenant isolation is the top guardrail; each layer (explicit WHERE, RLS, FK) is
tested independently so a regression in one is not masked by another.
"""
import psycopg
import pytest

from agents.db import get_connection
from agents.repos import (
    ConflictRepo,
    DealRepo,
    HandoffRepo,
    LeadRepo,
    TenantMismatchError,
    TenantSession,
)
from agents.schemas import ConflictItem, DealState, HandoffBrief, LeadProfile
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B


def lead(t, i="l1"):
    return LeadProfile(tenant_id=t, lead_id=i, company_name="Acme", confidence=0.7,
                       sources=["https://example.com/x"])


def conflict(t, i="c1", deal="d1"):
    return ConflictItem(tenant_id=t, conflict_id=i, deal_id=deal, field="amount", risk="high",
                        crm_value="1", evidence_value="2")


def deal(t, i="d1", **kw):
    return DealState(tenant_id=t, deal_id=i, stage="new", amount=5, **kw)


def test_round_trips(db):
    d = deal(A, lead=lead(A), conflicts=[conflict(A)])
    brief = HandoffBrief(tenant_id=A, deal_id="d1", summary="s", risks=["r"])
    with TenantSession(db, A) as s:
        LeadRepo(s).upsert(lead(A))
        DealRepo(s).save(d)
        HandoffRepo(s).upsert(brief)
        assert LeadRepo(s).get("l1") == lead(A)
        assert DealRepo(s).get("d1") == d
        assert HandoffRepo(s).get("d1") == brief
        assert ConflictRepo(s).list(status="pending") == [conflict(A)]
        assert DealRepo(s).list(stage="new") == [d]
        assert DealRepo(s).list(stage="won") == []


def test_upsert_updates(db):
    with TenantSession(db, A) as s:
        DealRepo(s).save(deal(A))
        DealRepo(s).save(deal(A).model_copy(update={"stage": "won"}))
        assert DealRepo(s).get("d1").stage == "won"


def test_repos_isolate_tenants_even_with_crafted_ids(db):
    with TenantSession(db, B) as s:
        LeadRepo(s).upsert(lead(B, "secret-lead"))
        DealRepo(s).save(deal(B, "secret-deal", conflicts=[conflict(B, "secret-c", "secret-deal")]))
        HandoffRepo(s).upsert(HandoffBrief(tenant_id=B, deal_id="secret-deal", summary="s"))
    with TenantSession(db, A) as s:
        assert LeadRepo(s).get("secret-lead") is None
        assert DealRepo(s).get("secret-deal") is None
        assert ConflictRepo(s).get("secret-c") is None
        assert HandoffRepo(s).get("secret-deal") is None
        assert LeadRepo(s).list() == [] and DealRepo(s).list() == []
        assert ConflictRepo(s).list() == []


def test_rls_fail_closed_without_tenant(db):
    with TenantSession(db, A) as s:
        DealRepo(s).save(deal(A))
    # Session exited: tenant reset. Raw SQL as app_user sees nothing...
    assert db.execute("SELECT count(*) FROM deals").fetchone()[0] == 0
    # ...and cannot write.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("INSERT INTO leads (tenant_id, lead_id, data) VALUES ('tenant-a','x','{}')")


def test_rls_backstop_without_where_clause(db):
    with TenantSession(db, A) as s:
        DealRepo(s).save(deal(A, "da"))
    with TenantSession(db, B) as s:
        DealRepo(s).save(deal(B, "db"))
    with TenantSession(db, A) as s:
        rows = s.execute("SELECT tenant_id FROM deals").fetchall()  # deliberately no WHERE
        assert rows == [(A,)]


def test_rls_blocks_writing_other_tenants_rows(db):
    with TenantSession(db, A) as s, pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        s.execute("INSERT INTO leads (tenant_id, lead_id, data) VALUES (%s,'x','{}')", (B,))


def test_cross_tenant_fk_rejected(db):
    with TenantSession(db, B) as s:
        DealRepo(s).save(deal(B, "only-b"))
    with TenantSession(db, A) as s, pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        ConflictRepo(s).upsert(conflict(A, "c-x", "only-b"))


def test_tenant_mismatch_raises_before_db(db):
    with TenantSession(db, A) as s:
        with pytest.raises(TenantMismatchError):
            LeadRepo(s).upsert(lead(B))
        with pytest.raises(TenantMismatchError):
            DealRepo(s).save(deal(B))


def test_session_requires_tenant(db):
    with pytest.raises(ValueError):
        TenantSession(db, "  ")


def test_tenant_setting_is_transaction_local(migrated_test_db):
    conn = get_connection(migrated_test_db)  # own connection: real commit happens
    try:
        with TenantSession(conn, A):
            pass
        assert conn.execute("SELECT current_setting('app.tenant_id', true)").fetchone()[0] in (
            "", None)
    finally:
        conn.close()
