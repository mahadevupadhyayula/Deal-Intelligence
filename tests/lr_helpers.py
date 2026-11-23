"""Helpers for Lead Research tests: scripted LLM, fixture loading, ICP seeding.

WHAT: `ScriptedLLM` (replays recorded structured outputs and records every prompt),
fixture loaders and an ICP seeding function.
WHERE: Used by the golden-dataset, graph, handler and API tests for Spec 005.
WHY: A scripted LLM + fixture search make agent runs deterministic and free. It is strict:
an unexpected extra LLM call fails the test, so call counts are verified, not assumed.
"""
import json
from pathlib import Path
from uuid import uuid4

from agents.evals.scripted import ScriptedLLM  # noqa: F401  (re-exported for tests)
from agents.lead_research.config import ResearchConfig
from agents.lead_research.tools import FixtureSearch, SearchResult
from agents.repos.async_session import AsyncTenantSession
from agents.repos.icp import IcpRepo
from agents.schemas.icp import IcpSegmentIn

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lead_research"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json") if not p.stem.startswith("icp_"))


def build_search(spec: dict) -> FixtureSearch:
    by_query = {q: [SearchResult(**r) for r in rs] for q, rs in spec["by_query"].items()}
    return FixtureSearch(by_query, [SearchResult(**r) for r in spec.get("default", [])])


def build_config(overrides: dict) -> ResearchConfig:
    return ResearchConfig(**{"retry_backoff_seconds": 0.0, **overrides})


async def seed_icp(pool, tenant_id: str, icp_file: str) -> None:
    for seg in json.loads((FIXTURES / icp_file).read_text()):
        async with pool.connection() as conn, AsyncTenantSession(conn, tenant_id) as s:
            await IcpRepo(s).create_version(seg["segment_key"], IcpSegmentIn.model_validate(seg["body"]))


def initial_state(tenant_id: str, inp: dict, job_id=None) -> dict:
    return {"tenant_id": tenant_id, "lead_id": inp["lead_id"], "job_id": str(job_id or uuid4()),
            "company_name": inp["company_name"], "domain": inp.get("domain"), "trace": []}
