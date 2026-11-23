"""Liveness and readiness probes.

WHAT: /healthz (process is up) and /readyz (Postgres + Redis reachable).
WHERE: Polled by Kubernetes (Spec 010) and load balancers; unauthenticated by design.
WHY: Split probes: liveness must not depend on backends (else a Redis blip restarts every
pod); readiness should, so traffic is withheld until dependencies work.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    checks: dict[str, str] = {}
    try:
        async with request.app.state.pool.connection() as conn:
            await conn.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        checks["postgres"] = "down"
    try:
        await request.app.state.arq.ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "down"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(checks, status_code=200 if ready else 503)
