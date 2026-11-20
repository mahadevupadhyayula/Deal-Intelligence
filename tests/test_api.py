"""Gate 1 tests for the FastAPI app (ASGI client, real test DB, fake ARQ).

WHAT: Auth, job submission, validation limits, tenant isolation, probes, async hygiene.
WHERE: Verifies `api/`.
WHY: The API is the trust boundary: it must authenticate, never leak across tenants, and
never do slow work inline. Redis is faked so enqueue arguments and failures are controllable.
"""
from pathlib import Path

import httpx
import psycopg
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from agents.security import hash_api_key
from api.main import create_app
from tests.conftest import TENANT_A as A


class FakeArq:
    def __init__(self):
        self.calls, self.fail = [], False

    async def enqueue_job(self, fn, *args, **kwargs):
        if self.fail:
            raise RedisConnectionError("redis down")
        self.calls.append((fn, args, kwargs))

    async def ping(self):
        if self.fail:
            raise RedisConnectionError("redis down")
        return True


@pytest.fixture
def arq():
    return FakeArq()


@pytest.fixture
async def client(apool, arq):
    app = create_app(pool=apool, arq=arq)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer bogus"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "},
])
async def test_bad_credentials_401(client, api_keys, headers):
    assert (await client.post("/v1/jobs", json={"kind": "echo"}, headers=headers)).status_code == 401


async def test_revoked_key_401(client, api_keys):
    r = await client.post("/v1/jobs", json={"kind": "echo"}, headers=auth(api_keys["revoked"]))
    assert r.status_code == 401


async def test_key_stored_as_hash_only(api_keys, admin_url):
    with psycopg.connect(admin_url) as c:
        hashes = {r[0] for r in c.execute("SELECT key_hash FROM api_keys")}
    assert api_keys[A] not in hashes and hash_api_key(api_keys[A]) in hashes


async def test_post_job_accepted_and_enqueued(client, api_keys, arq, admin_url):
    r = await client.post("/v1/jobs", json={"kind": "echo", "payload": {"a": 1}},
                          headers=auth(api_keys[A]))
    assert r.status_code == 202 and r.json()["status"] == "queued"
    jid = r.json()["job_id"]
    # Message carries ids only, never the payload.
    assert arq.calls == [("run_job", (A, jid), {"_job_id": jid})]
    with psycopg.connect(admin_url) as c:
        row = c.execute("SELECT tenant_id, status FROM jobs WHERE job_id = %s", (jid,)).fetchone()
    assert row == (A, "queued")


async def test_unknown_kind_422(client, api_keys):
    r = await client.post("/v1/jobs", json={"kind": "nope"}, headers=auth(api_keys[A]))
    assert r.status_code == 422


async def test_body_tenant_id_rejected(client, api_keys):
    r = await client.post("/v1/jobs", json={"kind": "echo", "tenant_id": "tenant-b"},
                          headers=auth(api_keys[A]))
    assert r.status_code == 422


async def test_oversize_payload_413(client, api_keys, arq):
    big = {"kind": "echo", "payload": {"x": "a" * 70_000}}
    r = await client.post("/v1/jobs", json=big, headers=auth(api_keys[A]))
    assert r.status_code == 413 and arq.calls == []


async def test_redis_down_503_and_job_failed(client, api_keys, arq, admin_url):
    arq.fail = True
    r = await client.post("/v1/jobs", json={"kind": "echo"}, headers=auth(api_keys[A]))
    assert r.status_code == 503
    with psycopg.connect(admin_url) as c:
        assert c.execute("SELECT status FROM jobs WHERE tenant_id = %s", (A,)).fetchone() == ("failed",)


async def test_get_job_and_cross_tenant_404(client, api_keys):
    jid = (await client.post("/v1/jobs", json={"kind": "echo"}, headers=auth(api_keys[A]))).json()["job_id"]
    mine = await client.get(f"/v1/jobs/{jid}", headers=auth(api_keys[A]))
    assert mine.status_code == 200 and mine.json()["status"] == "queued"
    theirs = await client.get(f"/v1/jobs/{jid}", headers=auth(api_keys["tenant-b"]))
    assert theirs.status_code == 404
    assert (await client.get(f"/v1/jobs/{jid}")).status_code == 401


async def test_probes(client, arq):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    arq.fail = True
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["redis"] == "down"
    assert (await client.get("/healthz")).status_code == 200  # liveness independent of Redis


def test_no_blocking_calls_in_async_paths():
    """Static guard for 'never block FastAPI': no sync DB/sleep/HTTP in api/ or workers/."""
    forbidden = ["psycopg.connect(", "time.sleep(", "requests.", "TenantSession(conn", "import requests"]
    root = Path(__file__).resolve().parent.parent
    for path in [*root.glob("api/**/*.py"), *root.glob("workers/**/*.py"),
                 *root.glob("agents/lead_research/**/*.py")]:
        text = path.read_text()
        for bad in forbidden:
            # AsyncTenantSession contains the substring "TenantSession(conn"; allow it.
            hits = [ln for ln in text.splitlines() if bad in ln and "AsyncTenantSession" not in ln]
            assert not hits, f"{path.name}: blocking pattern {bad!r}: {hits}"
