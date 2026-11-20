"""FastAPI application factory.

WHAT: `create_app()` wires routes plus the shared Postgres pool and ARQ Redis pool.
WHERE: Served by `uvicorn api.main:app`; clients and the Streamlit UI call it.
WHY: A factory with injectable pool/arq lets tests substitute fakes while production uses
lifespan-managed singletons (one pool per process, never per-request connections).
Injected resources are owned by the caller and not closed here.
"""
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from agents.config import get_settings
from api.routes import health, icp, jobs, leads


def create_app(pool: AsyncConnectionPool | None = None, arq=None) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_pool, own_arq = pool is None, arq is None
        if own_pool:
            app.state.pool = AsyncConnectionPool(
                settings.database_url.get_secret_value(), open=False)
            await app.state.pool.open()
        if own_arq:
            app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        yield
        if own_pool:
            await app.state.pool.close()
        if own_arq:
            await app.state.arq.aclose()

    app = FastAPI(title="Deal Intelligence API", lifespan=lifespan)
    app.state.max_payload_bytes = settings.job_payload_max_bytes
    if pool is not None:
        app.state.pool = pool
    if arq is not None:
        app.state.arq = arq
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(icp.router)
    app.include_router(leads.router)
    return app


app = create_app()
