"""TenantSession: binds a DB transaction to exactly one tenant.

WHAT: Context manager that opens a transaction and sets the transaction-local
`app.tenant_id` used by the RLS policies; also guards against cross-tenant writes.
WHERE: Created per request/agent-run by API handlers and workers; passed to repos.
WHY: `set_config(..., is_local=true)` dies with the transaction, so a pooled
connection can never carry one tenant's identity into the next checkout. Nested
use (tests run inside an outer transaction) uses a savepoint, where the setting
would otherwise linger, so we explicitly reset it on a clean exit.
"""
from typing import Self

import psycopg

from agents.schemas.base import TenantModel


class TenantMismatchError(ValueError):
    """A model's tenant_id differs from the session's tenant."""


class TenantSession:
    def __init__(self, conn: psycopg.Connection, tenant_id: str):
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        self.conn = conn
        self.tenant_id = tenant_id
        self._tx = None

    def __enter__(self) -> Self:
        self._tx = self.conn.transaction()
        self._tx.__enter__()
        self.conn.execute("SELECT set_config('app.tenant_id', %s, true)", (self.tenant_id,))
        return self

    def __exit__(self, exc_type, exc, tb):
        healthy = self.conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        if exc_type is None and healthy:
            # '' matches no tenant (tenant_id is CHECK <> ''), so this fails closed.
            self.conn.execute("SELECT set_config('app.tenant_id', '', true)")
        # On error the transaction/savepoint rollback reverts the setting itself.
        return self._tx.__exit__(exc_type, exc, tb)

    def execute(self, query: str, params: tuple = ()) -> psycopg.Cursor:
        return self.conn.execute(query, params)

    def require_own(self, model: TenantModel) -> None:
        """Reject a model from another tenant BEFORE touching the database."""
        if model.tenant_id != self.tenant_id:
            raise TenantMismatchError(
                f"{type(model).__name__} tenant_id does not match session tenant"
            )
