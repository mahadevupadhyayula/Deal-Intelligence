"""Gate 1 tests for the ICP API (ASGI client, real test DB, fake ARQ).

WHAT: Auth, create/list/get/versions/retire, validation errors, cross-tenant 404s.
WHERE: Verifies `api/routes/icp.py`.
WHY: Tenant identity must come only from the API key, and bad ICPs must be rejected with
field-level errors before they reach the database.
"""
import httpx
import pytest

from api.main import create_app
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
from tests.icp_helpers import example, judged


class FakeArq:
    async def enqueue_job(self, *a, **k):
        return None

    async def ping(self):
        return True


@pytest.fixture
async def client(apool):
    app = create_app(pool=apool, arq=FakeArq())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def small(name="x", rank="primary"):
    return {"name": name, "rank": rank, "criteria": [judged("a")]}


async def test_requires_auth(client, api_keys):
    assert (await client.get("/v1/icp/segments")).status_code == 401
    assert (await client.put("/v1/icp/segments/p", json=small())).status_code == 401
    r = await client.get("/v1/icp/segments", headers=auth(api_keys["revoked"]))
    assert r.status_code == 401


async def test_put_get_list_versions_retire(client, api_keys):
    h = auth(api_keys[A])
    r = await client.put("/v1/icp/segments/primary-seg", json=example("primary"), headers=h)
    assert r.status_code == 201 and r.json() == {"segment_key": "primary-seg", "version": 1}
    r = await client.put("/v1/icp/segments/secondary-seg", json=example("secondary"), headers=h)
    assert r.status_code == 201

    listed = (await client.get("/v1/icp/segments", headers=h)).json()
    assert [s["segment_key"] for s in listed] == ["primary-seg", "secondary-seg"]
    assert all("tenant_id" not in s for s in listed)

    got = (await client.get("/v1/icp/segments/primary-seg", headers=h)).json()
    assert got["version"] == 1 and len(got["criteria"]) == 10 and len(got["personas"]) == 2

    r = await client.put("/v1/icp/segments/primary-seg", json=small("v2"), headers=h)
    assert r.json()["version"] == 2
    versions = (await client.get("/v1/icp/segments/primary-seg/versions", headers=h)).json()
    assert [(v["version"], v["is_active"]) for v in versions] == [(2, True), (1, False)]
    old = await client.get("/v1/icp/segments/primary-seg/versions/1", headers=h)
    assert old.status_code == 200 and old.json()["name"].startswith("Founder-led")

    assert (await client.delete("/v1/icp/segments/secondary-seg", headers=h)).status_code == 204
    assert (await client.get("/v1/icp/segments/secondary-seg", headers=h)).status_code == 404
    assert [s["segment_key"] for s in (await client.get("/v1/icp/segments", headers=h)).json()] == [
        "primary-seg"]
    assert (await client.delete("/v1/icp/segments/secondary-seg", headers=h)).status_code == 404


async def test_validation_errors_422(client, api_keys):
    h = auth(api_keys[A])
    bad = {**small(), "criteria": [judged("a"), judged("a")]}  # duplicate ids
    r = await client.put("/v1/icp/segments/p", json=bad, headers=h)
    assert r.status_code == 422 and "unique" in r.text
    for extra in ({"tenant_id": B}, {"version": 9}, {"segment_key": "x"}):
        r = await client.put("/v1/icp/segments/p", json={**small(), **extra}, headers=h)
        assert r.status_code == 422, extra
    assert (await client.put("/v1/icp/segments/Bad_Key", json=small(), headers=h)).status_code == 422


async def test_missing_segment_404(client, api_keys):
    h = auth(api_keys[A])
    assert (await client.get("/v1/icp/segments/nope", headers=h)).status_code == 404
    assert (await client.get("/v1/icp/segments/nope/versions", headers=h)).status_code == 404
    assert (await client.get("/v1/icp/segments/nope/versions/1", headers=h)).status_code == 404
    assert (await client.get("/v1/icp/segments", headers=h)).json() == []


async def test_cross_tenant_isolation(client, api_keys):
    a, b = auth(api_keys[A]), auth(api_keys[B])
    await client.put("/v1/icp/segments/primary-seg", json=small("a-private"), headers=a)
    assert (await client.get("/v1/icp/segments", headers=b)).json() == []
    for path in ("", "/versions", "/versions/1"):
        r = await client.get(f"/v1/icp/segments/primary-seg{path}", headers=b)
        assert r.status_code == 404, path
    assert (await client.delete("/v1/icp/segments/primary-seg", headers=b)).status_code == 404
    # B writes its own version 1 without disturbing A's
    r = await client.put("/v1/icp/segments/primary-seg", json=small("b-own"), headers=b)
    assert r.json()["version"] == 1
    assert (await client.get("/v1/icp/segments/primary-seg", headers=a)).json()["name"] == "a-private"
