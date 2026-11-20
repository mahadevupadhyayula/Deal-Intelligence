"""Gate 1 tests for IcpRepo (as non-superuser app_user) and migration 0004.

WHAT: Versioning, single-active guarantee, retire, concurrency, tenant isolation, RLS.
WHERE: Verifies `agents/repos/icp.py` and `migrations/0004_icp_segments.sql`.
WHY: ICP versions are cited by evaluations, so they must be immutable, gapless and
unreadable across tenants.
"""
import asyncio

import psycopg
import pytest

from agents.repos.async_session import AsyncTenantSession
from agents.repos.icp import IcpRepo
from agents.schemas.icp import IcpSegmentIn
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
from tests.icp_helpers import example, judged

KEY = "primary-seg"


def body(name="v"):
    return IcpSegmentIn.model_validate(
        {"name": name, "rank": "primary", "criteria": [judged("a")]})


async def test_versions_increment_and_one_active(adb):
    async with AsyncTenantSession(adb, A) as s:
        repo = IcpRepo(s)
        v1 = await repo.create_version(KEY, body("one"))
        v2 = await repo.create_version(KEY, body("two"))
        assert (v1.version, v2.version) == (1, 2)
        assert (await repo.get_active(KEY)).name == "two"
        assert (await repo.get_version(KEY, 1)).name == "one"  # history stays readable
        versions = await repo.list_versions(KEY)
        assert [(sg.version, active) for sg, active in versions] == [(2, True), (1, False)]


async def test_db_enforces_single_active(adb):
    async with AsyncTenantSession(adb, A) as s:
        await IcpRepo(s).create_version(KEY, body())  # version 1 is active
        with pytest.raises(psycopg.errors.UniqueViolation):
            async with adb.transaction():  # savepoint so the error does not poison the session
                await s.execute(
                    "INSERT INTO icp_segments (tenant_id, segment_key, version, name, rank, "
                    "definition, is_active) VALUES (%s, %s, 99, 'x', 'primary', '{}', true)",
                    (A, KEY))


async def test_retire_keeps_history(adb):
    async with AsyncTenantSession(adb, A) as s:
        repo = IcpRepo(s)
        await repo.create_version(KEY, body())
        assert await repo.retire(KEY) is True
        assert await repo.get_active(KEY) is None
        assert await repo.get_version(KEY, 1) is not None
        assert await repo.retire(KEY) is False  # nothing active left


async def test_list_active_orders_primary_first(adb):
    async with AsyncTenantSession(adb, A) as s:
        repo = IcpRepo(s)
        sec = IcpSegmentIn.model_validate(
            {"name": "s", "rank": "secondary", "criteria": [judged("a")]})
        await repo.create_version("aaa-secondary", sec)
        await repo.create_version("zzz-primary", body())
        assert [x.rank for x in await repo.list_active()] == ["primary", "secondary"]


async def test_example_round_trips_through_db(adb):
    ex = IcpSegmentIn.model_validate(example("primary"))
    async with AsyncTenantSession(adb, A) as s:
        saved = await IcpRepo(s).create_version("hubspot", ex)
        loaded = await IcpRepo(s).get_active("hubspot")
    assert loaded.criteria == ex.criteria and loaded.personas == ex.personas
    assert loaded.tenant_id == A and saved.version == 1


async def test_cross_tenant_isolation(adb):
    async with AsyncTenantSession(adb, A) as s:
        await IcpRepo(s).create_version(KEY, body("a-only"))
    async with AsyncTenantSession(adb, B) as s:
        repo = IcpRepo(s)
        assert await repo.get_active(KEY) is None
        assert await repo.get_version(KEY, 1) is None
        assert await repo.list_active() == [] and await repo.list_versions(KEY) == []
        assert await repo.retire(KEY) is False
        b1 = await repo.create_version(KEY, body("b-own"))  # both tenants may have version 1
        assert b1.version == 1
    async with AsyncTenantSession(adb, A) as s:
        assert (await IcpRepo(s).get_active(KEY)).name == "a-only"


async def test_rls_fail_closed_without_tenant(adb):
    async with AsyncTenantSession(adb, A) as s:
        await IcpRepo(s).create_version(KEY, body())
    cur = await adb.execute("SELECT count(*) FROM icp_segments")
    assert (await cur.fetchone())[0] == 0


async def test_concurrent_creates_get_distinct_versions(apool):
    async def create(i):
        async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
            return (await IcpRepo(s).create_version(KEY, body(f"c{i}"))).version

    versions = await asyncio.gather(*(create(i) for i in range(6)))
    assert sorted(versions) == [1, 2, 3, 4, 5, 6]
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        active = [sg for sg, is_active in await IcpRepo(s).list_versions(KEY) if is_active]
    assert len(active) == 1 and active[0].version == max(versions)
