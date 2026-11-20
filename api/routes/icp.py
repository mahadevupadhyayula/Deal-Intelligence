"""ICP segment management endpoints.

WHAT: PUT/GET/DELETE for a tenant's versioned ICP segments.
WHERE: Used by operators now and the Streamlit ICP editor later (Spec 014); the Lead
Research agent reads the same data through `IcpRepo`.
WHY: PUT always creates a NEW version (history is immutable) and makes it active. Tenant
comes only from the API key; request bodies cannot carry tenant_id/version (extra=forbid),
and another tenant's segment is a 404, never a 403, so existence is not leaked.
"""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel

from agents.repos.async_session import AsyncTenantSession
from agents.repos.icp import IcpRepo
from agents.schemas.icp import IcpSegment, IcpSegmentIn, IcpSegmentOut
from api.auth import current_tenant

router = APIRouter(prefix="/v1/icp/segments")

SegmentKey = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


def _out(seg: IcpSegment) -> IcpSegmentOut:
    return IcpSegmentOut.model_validate(seg.model_dump(exclude={"tenant_id"}))


class SegmentCreated(BaseModel):
    segment_key: str
    version: int


class VersionSummary(BaseModel):
    version: int
    name: str
    rank: str
    is_active: bool
    created_at: datetime


@router.put("/{segment_key}", status_code=201, response_model=SegmentCreated)
async def put_segment(segment_key: SegmentKey, body: IcpSegmentIn, request: Request,
                      tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        seg = await IcpRepo(s).create_version(segment_key, body)
    return SegmentCreated(segment_key=seg.segment_key, version=seg.version)


@router.get("", response_model=list[IcpSegmentOut])
async def list_segments(request: Request, tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        return [_out(x) for x in await IcpRepo(s).list_active()]


@router.get("/{segment_key}", response_model=IcpSegmentOut)
async def get_segment(segment_key: SegmentKey, request: Request,
                      tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        seg = await IcpRepo(s).get_active(segment_key)
    if seg is None:
        raise HTTPException(404, "segment not found")
    return _out(seg)


@router.get("/{segment_key}/versions", response_model=list[VersionSummary])
async def list_versions(segment_key: SegmentKey, request: Request,
                        tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        rows = await IcpRepo(s).list_versions(segment_key)
    if not rows:
        raise HTTPException(404, "segment not found")
    return [VersionSummary(version=seg.version, name=seg.name, rank=seg.rank,
                           is_active=active, created_at=seg.created_at) for seg, active in rows]


@router.get("/{segment_key}/versions/{version}", response_model=IcpSegmentOut)
async def get_version(segment_key: SegmentKey, version: int, request: Request,
                      tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        seg = await IcpRepo(s).get_version(segment_key, version)
    if seg is None:
        raise HTTPException(404, "version not found")
    return _out(seg)


@router.delete("/{segment_key}", status_code=204)
async def retire_segment(segment_key: SegmentKey, request: Request,
                         tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        retired = await IcpRepo(s).retire(segment_key)
    if not retired:
        raise HTTPException(404, "segment not found")
    return Response(status_code=204)
