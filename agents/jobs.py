"""Job handler contract.

WHAT: The `JobContext` passed to handlers, the handler type, and the stub `echo` handler.
WHERE: Used by the worker (`workers/tasks.py`, `workers/handlers.py`); the API only needs
`agents/job_kinds.py`.
WHY: Handlers need runtime dependencies (DB pool, search tool, LLM). Passing them explicitly
in a context object keeps handlers testable (inject fakes) and avoids module-level globals.
Handlers get `(ctx, tenant_id, job_id, payload)` and must never read tenant data without a
tenant-bound session.
"""
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
    """Stub handler used to prove the plumbing end to end."""
    return {"echo": payload}
