"""FastAPI application factory.

WHAT: `create_app()` wires routes plus the shared Postgres pool and ARQ Redis pool.
WHERE: Served by `uvicorn api.main:app`; clients and the Streamlit UI call it.
WHY: A factory with injectable pool/arq lets tests substitute fakes while production uses
lifespan-managed singletons (one pool per process, never per-request connections).
Injected resources are owned by the caller and not closed here.
"""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from psycopg_pool import AsyncConnectionPool

from agents.config import get_settings
from agents.guardrails.limits import Limits
from agents.guardrails.redact import install_log_redaction
from agents.observability import metrics
from agents.observability.logging import bind_context, clear_context, configure_logging, log_event
from agents.observability.queue import QueueCollector
from api.reaper import reaper_loop
from api.routes import health, icp, jobs, leads

_log = logging.getLogger("api.access")


def _start_metrics(settings):
    """Metrics live on their own port (never the public one); the queue gauges are read at scrape
    time on the listener thread with a short-timeout sync Redis client."""
    if not settings.metrics_enabled:
        return None
    import redis as sync_redis

    client = sync_redis.Redis.from_url(settings.redis_url, socket_timeout=1,
                                       socket_connect_timeout=1)
    return metrics.start_metrics_server(settings.api_metrics_port, settings.metrics_bind_host,
                                        extra_collectors=[QueueCollector(client)])


async def _observe(request: Request, call_next):
    """One access line + HTTP metrics per request. O(1) work, no body or query string logged."""
    started = time.monotonic()
    rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
    clear_context()
    bind_context(request_id=rid)
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        elapsed = time.monotonic() - started
        metrics.observe_http(request.method, route, status, elapsed)
        # Probes fire every few seconds: keep them out of INFO so real traffic stays visible.
        level = logging.DEBUG if route in ("/healthz", "/readyz") else logging.INFO
        log_event(_log, "http.request", level, method=request.method, route=route, status=status,
                  duration_ms=round(elapsed * 1000),
                  tenant=getattr(request.state, "tenant_hash", None))
        clear_context()


def create_app(pool: AsyncConnectionPool | None = None, arq=None,
               limits: Limits | None = None) -> FastAPI:
    settings = get_settings()
    install_log_redaction()  # logs may echo provider errors; redact secret shapes (Spec 006)
    configure_logging("api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_pool, own_arq = pool is None, arq is None
        if own_pool:
            app.state.pool = AsyncConnectionPool(
                settings.database_url.get_secret_value(), open=False)
            await app.state.pool.open()
        if own_arq:
            app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        metrics_server = _start_metrics(settings)
        reaper = (asyncio.create_task(reaper_loop(app.state.pool, settings))
                  if settings.reaper_enabled else None)
        yield
        if reaper is not None:
            reaper.cancel()
        if metrics_server is not None:
            metrics_server.shutdown()
        if own_pool:
            await app.state.pool.close()
        if own_arq:
            await app.state.arq.aclose()

    app = FastAPI(title="Deal Intelligence API", lifespan=lifespan)
    app.state.max_payload_bytes = settings.job_payload_max_bytes
    app.state.limits = limits or Limits.from_settings(settings)
    if pool is not None:
        app.state.pool = pool
    if arq is not None:
        app.state.arq = arq
    app.middleware("http")(_observe)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(icp.router)
    app.include_router(leads.router)
    return app


app = create_app()
