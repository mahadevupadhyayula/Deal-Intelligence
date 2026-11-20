"""ARQ WorkerSettings.

WHAT: Configuration for `arq workers.settings.WorkerSettings`.
WHERE: Process entry point for workers; KEDA scales its pods on Redis queue depth (Spec 017).
WHY: One pool per worker process (opened at startup) shared by all jobs it runs.
`max_tries=1`: no automatic retries yet (see tasks.py).
"""
from typing import ClassVar

from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from agents.config import get_settings
from workers.tasks import run_job

_settings = get_settings()


async def startup(ctx: dict) -> None:
    ctx["pool"] = AsyncConnectionPool(_settings.database_url.get_secret_value(), open=False)
    await ctx["pool"].open()


async def shutdown(ctx: dict) -> None:
    await ctx["pool"].close()


class WorkerSettings:
    functions: ClassVar[list] = [run_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_tries = 1
