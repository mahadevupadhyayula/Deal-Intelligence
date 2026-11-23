"""Per-tenant request rate limit, active-job cap and daily quotas.

WHAT: `Limits` (configuration), `RateLimiter` (Redis fixed window), and DB-backed checks
`check_admission` / `check_quota`, each returning a `Rejection` or None.
WHERE: `api/routes/jobs.py` (admission, before a job row is created) and `workers/tasks.py`
(quota re-check at run start, because jobs can wait in the queue).
WHY: One tenant must not be able to flood the queue or burn the shared provider budget.
Trade-offs: a fixed window allows a burst of up to 2x the limit across a window edge (simple,
one atomic INCR; a sliding window is a later upgrade). The active-job count and the quota read
run under a per-tenant advisory lock held by the caller, so concurrent submissions cannot
jointly exceed the cap. Quotas are checked before a run, not during it, so a tenant can exceed
a daily quota by at most the per-run caps of the jobs already in flight.
"""
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agents.repos.async_session import AsyncTenantSession


@dataclass(frozen=True)
class Limits:
    rate_per_minute: int = 30
    max_active_jobs: int = 10
    daily_llm_calls: int = 500
    daily_searches: int = 500

    @classmethod
    def from_settings(cls, s) -> "Limits":
        return cls(s.rate_limit_jobs_per_minute, s.tenant_max_active_jobs,
                   s.tenant_daily_max_llm_calls, s.tenant_daily_max_searches)


@dataclass(frozen=True)
class Rejection:
    code: str          # rate_limited | too_many_active_jobs | daily_quota_exceeded
    message: str
    retry_after: int   # seconds


def utc_today(now: datetime | None = None):
    return (now or datetime.now(UTC)).date()


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(1, math.ceil((nxt - now).total_seconds()))


class RateLimiter:
    """Fixed-window counter in Redis. `redis` needs async `incr` and `expire` (ArqRedis has both)."""

    def __init__(self, redis, per_minute: int, clock=time.time):
        self.redis, self.per_minute, self.clock = redis, per_minute, clock

    async def check(self, tenant_id: str) -> Rejection | None:
        now = self.clock()
        window = int(now // 60)
        key = f"rl:{tenant_id}:{window}"  # tenant in the key: counters are per tenant
        n = await self.redis.incr(key)
        if n == 1:
            await self.redis.expire(key, 120)
        if n > self.per_minute:
            return Rejection("rate_limited", "rate limit exceeded", max(1, math.ceil(60 - now % 60)))
        return None


async def check_quota(session: AsyncTenantSession, limits: Limits,
                      now: datetime | None = None) -> Rejection | None:
    cur = await session.execute(
        "SELECT llm_calls, searches FROM tenant_usage WHERE tenant_id = %s AND day = %s",
        (session.tenant_id, utc_today(now)))
    row = await cur.fetchone()
    llm, searches = row if row else (0, 0)
    if llm >= limits.daily_llm_calls or searches >= limits.daily_searches:
        return Rejection("daily_quota_exceeded", "daily usage quota exceeded",
                         seconds_until_utc_midnight(now))
    return None


async def check_admission(session: AsyncTenantSession, limits: Limits, provider_kind: bool,
                          now: datetime | None = None) -> Rejection | None:
    """Caller must hold the per-tenant advisory lock (see `lock_tenant`)."""
    cur = await session.execute(
        "SELECT count(*) FROM jobs WHERE tenant_id = %s AND status IN ('queued', 'running')",
        (session.tenant_id,))
    (active,) = await cur.fetchone()
    if active >= limits.max_active_jobs:
        return Rejection("too_many_active_jobs", "too many active jobs; retry shortly", 30)
    if provider_kind:
        return await check_quota(session, limits, now)
    return None


async def lock_tenant(session: AsyncTenantSession) -> None:
    """Serialise admission per tenant for the rest of this transaction."""
    await session.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (session.tenant_id,))
