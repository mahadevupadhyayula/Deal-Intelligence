"""Usage metering: count provider calls per job and persist them per tenant per day.

WHAT: `UsageMeter` (in-memory counters), `MeteredSearch` / `MeteredLLM` (decorators that count
every attempt), and `UsageRepo` (atomic daily upsert).
WHERE: The worker builds one meter per job, wraps the search/LLM adapters before handing them
to the handler, and flushes the meter after the job ends (workers/tasks.py).
WHY: Decorating the adapters counts every attempt, retries included, and failed calls (they
cost money too) with no change to agent code. Flushing happens in the worker AFTER the outcome,
outside the handler's timeout block, so a timed-out or crashed run is still billed.
"""
from dataclasses import dataclass
from datetime import date

from agents.repos.async_session import AsyncTenantSession


@dataclass
class UsageMeter:
    llm_calls: int = 0
    searches: int = 0


class MeteredSearch:
    def __init__(self, inner, meter: UsageMeter):
        self._inner, self._meter = inner, meter

    async def search(self, query: str, max_results: int = 5):
        self._meter.searches += 1  # counted before the call: a failed call still costs
        return await self._inner.search(query, max_results)


class MeteredLLM:
    def __init__(self, inner, meter: UsageMeter):
        self._inner, self._meter = inner, meter

    async def generate(self, schema, system: str, user: str):
        self._meter.llm_calls += 1
        return await self._inner.generate(schema, system, user)


class UsageRepo:
    def __init__(self, session: AsyncTenantSession):
        self.s = session

    async def add(self, day: date, llm_calls: int, searches: int) -> None:
        await self.s.execute(
            "INSERT INTO tenant_usage (tenant_id, day, llm_calls, searches) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, day) DO UPDATE SET "
            "llm_calls = tenant_usage.llm_calls + EXCLUDED.llm_calls, "
            "searches = tenant_usage.searches + EXCLUDED.searches, updated_at = now()",
            (self.s.tenant_id, day, llm_calls, searches))

    async def get(self, day: date) -> tuple[int, int]:
        cur = await self.s.execute(
            "SELECT llm_calls, searches FROM tenant_usage WHERE tenant_id = %s AND day = %s",
            (self.s.tenant_id, day))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (0, 0)
