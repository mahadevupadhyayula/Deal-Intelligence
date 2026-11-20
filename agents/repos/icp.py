"""IcpRepo: async persistence for versioned ICP segments.

WHAT: create immutable versions, read active/historical versions, retire a segment.
WHERE: Used by `api/routes/icp.py` (editing) and the Lead Research agent (loading active
segments for evaluation, Spec 005).
WHY: An advisory lock per (tenant, segment) serialises concurrent writers so versions are
gapless and distinct without retry loops; the partial unique index in migration 0004 is the
backstop. Versions are never updated in place (only `is_active` flips), preserving history.
"""
from psycopg.types.json import Jsonb

from agents.repos.async_session import AsyncTenantSession
from agents.schemas.icp import IcpSegment, IcpSegmentIn

_SELECT = (
    "SELECT tenant_id, segment_key, version, created_at, definition FROM icp_segments"
)


def _assemble(row) -> IcpSegment:
    tenant_id, segment_key, version, created_at, definition = row
    return IcpSegment.model_validate({
        **definition, "tenant_id": tenant_id, "segment_key": segment_key,
        "version": version, "created_at": created_at,
    })


class IcpRepo:
    def __init__(self, session: AsyncTenantSession):
        self.s = session

    async def create_version(self, segment_key: str, body: IcpSegmentIn) -> IcpSegment:
        # Lock is released at transaction end, i.e. after the new version is visible.
        await self.s.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{self.s.tenant_id}:{segment_key}",),
        )
        cur = await self.s.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM icp_segments "
            "WHERE tenant_id = %s AND segment_key = %s", (self.s.tenant_id, segment_key))
        version = (await cur.fetchone())[0]
        await self.s.execute(
            "UPDATE icp_segments SET is_active = false "
            "WHERE tenant_id = %s AND segment_key = %s AND is_active",
            (self.s.tenant_id, segment_key))
        cur = await self.s.execute(
            "INSERT INTO icp_segments (tenant_id, segment_key, version, name, rank, definition, "
            "is_active) VALUES (%s, %s, %s, %s, %s, %s, true) RETURNING created_at",
            (self.s.tenant_id, segment_key, version, body.name, body.rank,
             Jsonb(body.model_dump(mode="json"))))
        created_at = (await cur.fetchone())[0]
        return IcpSegment.model_validate({
            **body.model_dump(), "tenant_id": self.s.tenant_id, "segment_key": segment_key,
            "version": version, "created_at": created_at})

    async def get_active(self, segment_key: str) -> IcpSegment | None:
        cur = await self.s.execute(
            f"{_SELECT} WHERE tenant_id = %s AND segment_key = %s AND is_active",
            (self.s.tenant_id, segment_key))
        row = await cur.fetchone()
        return _assemble(row) if row else None

    async def list_active(self) -> list[IcpSegment]:
        cur = await self.s.execute(
            f"{_SELECT} WHERE tenant_id = %s AND is_active "
            "ORDER BY (definition->>'rank') = 'primary' DESC, segment_key",
            (self.s.tenant_id,))
        return [_assemble(r) for r in await cur.fetchall()]

    async def get_version(self, segment_key: str, version: int) -> IcpSegment | None:
        cur = await self.s.execute(
            f"{_SELECT} WHERE tenant_id = %s AND segment_key = %s AND version = %s",
            (self.s.tenant_id, segment_key, version))
        row = await cur.fetchone()
        return _assemble(row) if row else None

    async def list_versions(self, segment_key: str) -> list[tuple[IcpSegment, bool]]:
        """All versions, newest first, each with its is_active flag."""
        cur = await self.s.execute(
            f"{_SELECT.replace('definition', 'definition, is_active')} "
            "WHERE tenant_id = %s AND segment_key = %s ORDER BY version DESC",
            (self.s.tenant_id, segment_key))
        return [(_assemble(r[:5]), r[5]) for r in await cur.fetchall()]

    async def retire(self, segment_key: str) -> bool:
        cur = await self.s.execute(
            "UPDATE icp_segments SET is_active = false "
            "WHERE tenant_id = %s AND segment_key = %s AND is_active",
            (self.s.tenant_id, segment_key))
        return cur.rowcount == 1
