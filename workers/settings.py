"""ARQ WorkerSettings.

WHAT: Configuration for `arq workers.settings.WorkerSettings`.
WHERE: Process entry point for workers; KEDA scales its pods on Redis queue depth (Spec 010).
WHY: One DB pool plus one search/LLM client per worker process (opened at startup) shared by
all jobs it runs. Search/LLM are only built when configured, so a worker without research
credentials still starts and runs other kinds; `lead_research` jobs then fail with a clear
message. `max_tries=1`: no automatic retries yet (see tasks.py). The function-level timeout
is ARQ's OUTER limit; `run_job` enforces a shorter inner one.
Spec 009: JSON logging and a Prometheus listener (daemon thread) start in `startup`.
"""
from typing import ClassVar

from arq import func
from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from agents.config import get_settings
from agents.guardrails.redact import install_log_redaction, register_secret
from agents.guardrails.tracing import configure_trace_redaction
from agents.observability.logging import configure_logging
from agents.observability.metrics import WORKER_UP, start_metrics_server
from workers.tasks import run_job

_settings = get_settings()


def _register_secrets() -> None:
    """Make known secret values redactable wherever they appear (logs, errors, traces)."""
    for secret in (_settings.database_url, _settings.tavily_api_key, _settings.admin_database_url,
                   _settings.app_user_password):
        if secret is not None:
            register_secret(secret.get_secret_value())
    import os
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY"):
        register_secret(os.environ.get(name))


async def startup(ctx: dict) -> None:
    install_log_redaction()
    configure_logging("worker")  # after arq's own logging setup, so ours wins
    if _settings.metrics_enabled:
        ctx["metrics_server"] = start_metrics_server(
            _settings.worker_metrics_port, _settings.metrics_bind_host)
        WORKER_UP.set(1)
    _register_secrets()
    if _settings.trace_redaction:
        configure_trace_redaction()
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
    WORKER_UP.set(0)
    if ctx.get("metrics_server") is not None:
        ctx["metrics_server"].shutdown()
    await ctx["pool"].close()


class WorkerSettings:
    functions: ClassVar[list] = [func(run_job, timeout=_settings.job_timeout_seconds)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_tries = 1
    # Lets `arq --check` (the container healthcheck) see a fresh heartbeat; the default is hourly.
    health_check_interval = 30
