"""Job kind registry.

WHAT: Maps a job `kind` string to the async handler that performs it.
WHERE: Shared by the API (rejects unknown kinds up front) and the worker (dispatch).
WHY: One registry keeps the two sides in sync. Specs 004-008 register their agents here;
handlers get `(tenant_id, job_id, payload)` and must never read tenant data without
a tenant-bound session.
"""
from collections.abc import Awaitable, Callable
from typing import Any

JobHandler = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def echo(tenant_id: str, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Stub handler used to prove the plumbing end to end."""
    return {"echo": payload}


JOB_HANDLERS: dict[str, JobHandler] = {"echo": echo}
