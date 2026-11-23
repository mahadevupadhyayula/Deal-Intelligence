"""Call the stale-job reaper function.

WHAT: `reap_stale_jobs(pool, max_age_seconds)` -> list of (tenant_id, job_id) it failed.
WHERE: Used by the API's background reaper loop (`api/reaper.py`) and by tests.
WHY: The only cross-tenant operation in the app. It runs on a plain pooled connection (no
tenant session, because it spans tenants) and calls the SECURITY DEFINER function from
migration 0007, which is the sole holder of that privilege. The result is ids only.
"""


async def reap_stale_jobs(pool, max_age_seconds: float) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT tenant_id, job_id FROM reap_stale_jobs(make_interval(secs => %s))",
                                 (float(max_age_seconds),))
        return [(t, str(j)) for t, j in await cur.fetchall()]
