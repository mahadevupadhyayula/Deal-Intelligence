"""ARQ WorkerSettings.

WHAT: Configuration for `arq workers.settings.WorkerSettings`.
WHERE: Process entry point for workers; KEDA scales its pods on Redis queue depth (Spec 010).
WHY: One DB pool plus one search/LLM client per worker process (opened at startup) shared by
all jobs it runs. Search/LLM are only built when configured, so a worker without research
credentials still starts and runs other kinds; `lead_research` jobs then fail with a clear
message. `max_tries=1`: no automatic retries yet (see tasks.py). The function-level timeout
is ARQ's OUTER limit; `run_job` enforces a shorter inner one.
"""
from typing import ClassVar

from arq import func
from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from agents.config import get_settings
from workers.tasks import run_job

_settings = get_settings()


async def startup(ctx: dict) -> None:
    ctx["pool"] = AsyncConnectionPool(_settings.database_url.get_secret_value(), open=False)
    await ctx["pool"].open()
    ctx["run_timeout"] = _settings.research_run_timeout_seconds
    if _settings.tavily_api_key is not None and _settings.llm_model:
        # Imported here so the worker module itself stays light until research is configured.
        from agents.lead_research.llm import LangChainLLM
        from agents.lead_research.tools import TavilySearch

        ctx["search"] = TavilySearch(_settings.tavily_api_key.get_secret_value())
        ctx["llm"] = LangChainLLM(_settings.llm_model)


async def shutdown(ctx: dict) -> None:
    await ctx["pool"].close()


class WorkerSettings:
    functions: ClassVar[list] = [func(run_job, timeout=_settings.job_timeout_seconds)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_tries = 1
