"""AsyncTenantSession: async twin of TenantSession.

WHAT: Opens a transaction on a psycopg AsyncConnection and sets the transaction-local
`app.tenant_id` used by RLS.
WHERE: Used by the FastAPI handlers and ARQ worker (both run on an event loop).
WHY: Blocking DB calls inside `async def` handlers would stall every concurrent request
(violates "never block FastAPI"), so the async paths need a non-blocking session. Logic
mirrors the sync version deliberately: same RLS contract, same reset-on-clean-exit.
"""
from typing import Self

import psycopg

from agents.repos.session import TenantMismatchError
from agents.schemas.base import TenantModel


class AsyncTenantSession:
    def __init__(self, conn: psycopg.AsyncConnection, tenant_id: str):
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        self.conn = conn
        self.tenant_id = tenant_id
        self._tx = None

    async def __aenter__(self) -> Self:
        self._tx = self.conn.transaction()
        await self._tx.__aenter__()
        await self.conn.execute("SELECT set_config('app.tenant_id', %s, true)", (self.tenant_id,))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        healthy = self.conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        if exc_type is None and healthy:
            await self.conn.execute("SELECT set_config('app.tenant_id', '', true)")
        return await self._tx.__aexit__(exc_type, exc, tb)

    async def execute(self, query: str, params: tuple = ()) -> psycopg.AsyncCursor:
        return await self.conn.execute(query, params)

    def require_own(self, model: TenantModel) -> None:
        if model.tenant_id != self.tenant_id:
            raise TenantMismatchError(
                f"{type(model).__name__} tenant_id does not match session tenant"
            )
