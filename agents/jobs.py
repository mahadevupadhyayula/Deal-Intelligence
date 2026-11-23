"""Job handler contract.

WHAT: The `JobContext` passed to handlers, the handler type, and the stub `echo` handler.
WHERE: Used by the worker (`workers/tasks.py`, `workers/handlers.py`); the API only needs
`agents/job_kinds.py`.
WHY: Handlers need runtime dependencies (DB pool, search tool, LLM). Passing them explicitly
in a context object keeps handlers testable (inject fakes) and avoids module-level globals.
Handlers get `(ctx, tenant_id, job_id, payload)` and must never read tenant data without a
tenant-bound session.
"""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class JobContext:
    pool: Any                     # psycopg_pool.AsyncConnectionPool
    search: Any | None = None     # agents.lead_research.tools.SearchTool
    llm: Any | None = None        # agents.lead_research.llm.StructuredLLM


JobHandler = Callable[[JobContext, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def echo(ctx: JobContext, tenant_id: str, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Stub handler used to prove the plumbing end to end.

    Optional `delay_seconds` makes a free, slow job so deployment tests can watch queue depth,
    scale-out and graceful shutdown without spending provider money (Spec 008, used by 010).
    It is clamped to `ECHO_MAX_DELAY_SECONDS`, which defaults to 0 (disabled): a tenant must not
    be able to hold a shared worker slot in production just because the stub accepts a delay.
    """
    from agents.config import get_settings

    cap = get_settings().echo_max_delay_seconds
    delay = payload.get("delay_seconds")
    if cap > 0 and isinstance(delay, int | float) and not isinstance(delay, bool) and delay > 0:
        await asyncio.sleep(min(float(delay), cap))
    return {"echo": payload}
