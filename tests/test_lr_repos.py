"""Gate 1 tests for AsyncLeadRepo and AsyncEvaluationRepo (as non-superuser app_user).

WHAT: Round trips, insert-only history, idempotent create, ranked/paginated latest list, RLS.
WHERE: Verifies `agents/repos/lead_async.py`, `agents/repos/evaluation.py`, migration 0005.
WHY: The ranked list is how users find strong fits; pagination must be stable and tenant-safe.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from agents.repos.async_session import AsyncTenantSession
from agents.repos.evaluation import AsyncEvaluationRepo, decode_cursor, encode_cursor
from agents.repos.lead_async import AsyncLeadRepo
from agents.schemas import IcpEvaluation, LeadProfile
from agents.schemas.icp import SegmentScore
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def evaluation(tenant, lead, score, tier="strong", rank="primary", minutes=0, job=None, seg="primary-seg"):
    return IcpEvaluation(
        tenant_id=tenant, evaluation_id=uuid4(), lead_id=lead, job_id=job or uuid4(),
        icp_versions={seg: 1}, best_segment_key=seg, best_rank=rank, score=score, tier=tier,
        segments=[SegmentScore(tenant_id=tenant, segment_key=seg, version=1, rank=rank, score=score,
                               tier=tier, evidence_ratio=1.0)],
        verdicts={seg: []}, entity_confirmed=True, status="complete",
        created_at=T0 + timedelta(minutes=minutes))


def lead(tenant, lead_id, name="Acme"):
    return LeadProfile(tenant_id=tenant, lead_id=lead_id, company_name=name, confidence=0.5)


async def test_lead_round_trip_and_overwrite(adb):
    async with AsyncTenantSession(adb, A) as s:
        repo = AsyncLeadRepo(s)
        await repo.upsert(lead(A, "l1", "One"))
        await repo.upsert(lead(A, "l1", "One v2"))
        assert (await repo.get("l1")).company_name == "One v2"
        assert await repo.get("nope") is None


async def test_evaluation_history_latest_and_idempotent_create(adb):
    job = uuid4()
    async with AsyncTenantSession(adb, A) as s:
        repo = AsyncEvaluationRepo(s)
        e1, e2 = evaluation(A, "l1", 40, "weak", minutes=1, job=job), evaluation(A, "l1", 90, minutes=5)
        assert await repo.create(e1) is True
        assert await repo.create(evaluation(A, "l1", 1, job=job)) is False  # same job: no-op
        assert await repo.create(e2) is True
        assert (await repo.get_latest("l1")) == e2
        assert [x.score for x in await repo.list("l1")] == [90, 40]  # newest first, e1 kept


async def test_list_latest_ranks_filters_and_paginates(adb):
    async with AsyncTenantSession(adb, A) as s:
        repo = AsyncEvaluationRepo(s)
        for lid, name in (("a", "Alpha"), ("b", "Beta"), ("c", "Gamma"), ("d", "Delta")):
            await AsyncLeadRepo(s).upsert(lead(A, lid, name))
        await repo.create(evaluation(A, "a", 10, "not_fit", minutes=1))
        await repo.create(evaluation(A, "a", 95, "strong", minutes=9))       # latest for a
        await repo.create(evaluation(A, "b", 80, "strong"))
        await repo.create(evaluation(A, "c", 80, "strong", rank="secondary", seg="secondary-seg"))
        await repo.create(evaluation(A, "d", 30, "weak"))

        items, nxt = await repo.list_latest()
        assert [(i["lead_id"], i["score"]) for i in items] == [("a", 95), ("b", 80), ("c", 80), ("d", 30)]
        assert items[0]["company_name"] == "Alpha" and nxt is None
        assert [i["lead_id"] for i in (await repo.list_latest(tier="strong"))[0]] == ["a", "b", "c"]
        assert [i["lead_id"] for i in (await repo.list_latest(best_rank="secondary"))[0]] == ["c"]

        page1, c1 = await repo.list_latest(limit=2)
        page2, c2 = await repo.list_latest(limit=2, cursor=c1)
        assert [i["lead_id"] for i in page1 + page2] == ["a", "b", "c", "d"] and c1 and c2 is None
        # stable while a new, higher-scoring evaluation arrives between pages
        await repo.create(evaluation(A, "e", 99, "strong"))
        page2b, _ = await repo.list_latest(limit=2, cursor=c1)
        assert [i["lead_id"] for i in page2b] == ["c", "d"]


async def test_cursor_round_trip_and_garbage():
    assert decode_cursor(encode_cursor(12.5, "lead-x")) == (12.5, "lead-x")
    for bad in ("not-base64!!", "e30="):
        with pytest.raises(ValueError):
            decode_cursor(bad)


async def test_tenant_isolation_and_rls(adb):
    async with AsyncTenantSession(adb, A) as s:
        await AsyncLeadRepo(s).upsert(lead(A, "secret"))
        await AsyncEvaluationRepo(s).create(evaluation(A, "secret", 90))
    async with AsyncTenantSession(adb, B) as s:
        assert await AsyncLeadRepo(s).get("secret") is None
        assert await AsyncEvaluationRepo(s).get_latest("secret") is None
        assert await AsyncEvaluationRepo(s).list("secret") == []
        assert (await AsyncEvaluationRepo(s).list_latest())[0] == []
        with pytest.raises(Exception, match="tenant"):
            await AsyncEvaluationRepo(s).create(evaluation(A, "x", 1))  # foreign model rejected
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with adb.transaction():  # savepoint: the expected error must not poison the session
                await s.execute(
                    "INSERT INTO lead_icp_evaluations (tenant_id, evaluation_id, lead_id, job_id, "
                    "best_segment_key, best_rank, score, tier, status, data) VALUES "
                    "(%s, %s, 'x', %s, 'p', 'primary', 1, 'weak', 'complete', '{}')",
                    (A, uuid4(), uuid4()))  # writing as tenant B's session into tenant A: RLS blocks
    cur = await adb.execute("SELECT count(*) FROM lead_icp_evaluations")  # no tenant set: fail closed
    assert (await cur.fetchone())[0] == 0

