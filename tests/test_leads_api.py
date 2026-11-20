"""Gate 1 tests for the leads read API (ASGI client, real test DB).

WHAT: Auth, profile + latest evaluation, history, ranked/filtered/paginated list, cross-tenant 404s,
no tenant_id leakage, parameter validation.
WHERE: Verifies `api/routes/leads.py`.
WHY: This is how users find their best-fit leads; it must rank correctly and never cross tenants.
"""
import httpx
import pytest

from agents.repos.async_session import AsyncTenantSession
from agents.repos.evaluation import AsyncEvaluationRepo
from agents.repos.lead_async import AsyncLeadRepo
from api.main import create_app
from tests.conftest import TENANT_A as A
from tests.conftest import TENANT_B as B
from tests.test_lr_repos import evaluation, lead


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


async def seed(apool):
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        for lid, name, score, tier, rank, minutes in (
            ("l1", "Alpha", 95, "strong", "primary", 5), ("l2", "Beta", 70, "moderate", "primary", 1),
            ("l3", "Gamma", 70, "moderate", "secondary", 1), ("l4", "Delta", 10, "not_fit", "primary", 1),
        ):
            await AsyncLeadRepo(s).upsert(lead(A, lid, name))
            await AsyncEvaluationRepo(s).create(evaluation(A, lid, score, tier, rank, minutes))
        await AsyncEvaluationRepo(s).create(evaluation(A, "l1", 20, "weak", minutes=0))  # older history


async def test_requires_auth(client, api_keys):
    for path in ("/v1/leads", "/v1/leads/l1", "/v1/leads/l1/evaluations"):
        assert (await client.get(path)).status_code == 401


async def test_get_lead_and_history_without_tenant_leak(client, api_keys, apool):
    await seed(apool)
    h = auth(api_keys[A])
    r = await client.get("/v1/leads/l1", headers=h)
    body = r.json()
    assert r.status_code == 200 and body["profile"]["company_name"] == "Alpha"
    assert body["evaluation"]["score"] == 95 and body["evaluation"]["tier"] == "strong"
    assert "tenant_id" not in r.text  # stripped recursively, including nested models
    hist = (await client.get("/v1/leads/l1/evaluations", headers=h)).json()
    assert [e["score"] for e in hist] == [95, 20]


async def test_missing_and_cross_tenant_lead_404(client, api_keys, apool):
    await seed(apool)
    assert (await client.get("/v1/leads/nope", headers=auth(api_keys[A]))).status_code == 404
    b = auth(api_keys[B])
    assert (await client.get("/v1/leads/l1", headers=b)).status_code == 404
    assert (await client.get("/v1/leads/l1/evaluations", headers=b)).status_code == 404
    assert (await client.get("/v1/leads", headers=b)).json() == {"items": [], "next_cursor": None}


async def test_ranked_list_filters_and_pagination(client, api_keys, apool):
    await seed(apool)
    h = auth(api_keys[A])
    body = (await client.get("/v1/leads", headers=h)).json()
    assert [i["lead_id"] for i in body["items"]] == ["l1", "l2", "l3", "l4"]
    assert body["items"][0]["company_name"] == "Alpha" and body["items"][0]["tier"] == "strong"
    assert [i["lead_id"] for i in (await client.get("/v1/leads?tier=moderate", headers=h)).json()["items"]] == ["l2", "l3"]
    assert [i["lead_id"] for i in (await client.get("/v1/leads?best_rank=secondary", headers=h)).json()["items"]] == ["l3"]

    seen, cursor = [], None
    for _ in range(5):
        url = "/v1/leads?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url, headers=h)).json()
        seen += [i["lead_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ["l1", "l2", "l3", "l4"]


@pytest.mark.parametrize("query", ["tier=amazing", "limit=0", "limit=201", "best_rank=tertiary", "cursor=%25%25bad"])
async def test_bad_parameters_422(client, api_keys, query):
    assert (await client.get(f"/v1/leads?{query}", headers=auth(api_keys[A]))).status_code == 422
