"""Bearer API-key authentication -> tenant_id.

WHAT: FastAPI dependency `current_tenant` that maps `Authorization: Bearer <key>` to a tenant.
WHERE: Injected into every /v1 route; its output is the ONLY source of tenant identity.
WHY: Taking tenant_id from a header/body would be spoofable. A hashed-key lookup ties the
request to a tenant server-side. All failures return the same 401 (no key enumeration).
"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agents.observability.logging import bind_context, hash_tenant
from agents.security import hash_api_key

_bearer = HTTPBearer(auto_error=False)


async def current_tenant(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008 - standard FastAPI idiom
) -> str:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key",
                                 headers={"WWW-Authenticate": "Bearer"})
    if creds is None or not creds.credentials:
        raise unauthorized
    # api_keys is not under RLS (tenant unknown yet), so a plain pooled connection is used.
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT tenant_id FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
            (hash_api_key(creds.credentials),),
        )
        row = await cur.fetchone()
    if row is None:
        raise unauthorized
    # Hashed tenant for logs: the access line reads it from request.state; handler logs from context.
    request.state.tenant_hash = hash_tenant(row[0])
    bind_context(tenant=request.state.tenant_hash)
    return row[0]
