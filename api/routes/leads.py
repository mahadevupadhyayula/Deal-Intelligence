"""Lead read endpoints: profile + latest evaluation, history, and the ranked list.

WHAT: GET /v1/leads/{id}, GET /v1/leads/{id}/evaluations, GET /v1/leads (ranked by fit).
WHERE: Read side of the Lead Research agent (Spec 005); the Streamlit UI (Spec 014) reads it.
WHY: Ranking leads by ICP fit is the point of the feature, so the list is sorted by score with
keyset pagination (stable while new evaluations arrive) and tier/rank filters. Tenant comes only
from the API key; another tenant's lead is a 404. `tenant_id` is stripped from responses
(callers know their own tenant), recursively because nested models carry it too.
"""
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from agents.repos.async_session import AsyncTenantSession
from agents.repos.evaluation import AsyncEvaluationRepo, decode_cursor
from agents.repos.lead_async import AsyncLeadRepo
from api.auth import current_tenant

router = APIRouter(prefix="/v1/leads")

LeadId = Annotated[str, Path(min_length=1, max_length=128)]


def strip_tenant(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_tenant(v) for k, v in obj.items() if k != "tenant_id"}
    if isinstance(obj, list):
        return [strip_tenant(v) for v in obj]
    return obj


@router.get("")
async def list_leads(
    request: Request,
    tier: Literal["strong", "moderate", "weak", "not_fit", "insufficient_data"] | None = None,
    best_rank: Literal["primary", "secondary"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    tenant_id: str = Depends(current_tenant),
):
    if cursor:
        try:
            decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(422, "invalid cursor") from exc
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        items, next_cursor = await AsyncEvaluationRepo(s).list_latest(tier, best_rank, limit, cursor)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/{lead_id}")
async def get_lead(lead_id: LeadId, request: Request,
                   tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        profile = await AsyncLeadRepo(s).get(lead_id)
        evaluation = await AsyncEvaluationRepo(s).get_latest(lead_id)
    if profile is None:
        raise HTTPException(404, "lead not found")
    return {
        "profile": strip_tenant(profile.model_dump(mode="json")),
        "evaluation": strip_tenant(evaluation.model_dump(mode="json")) if evaluation else None,
    }


@router.get("/{lead_id}/evaluations")
async def list_evaluations(lead_id: LeadId, request: Request,
                           tenant_id: str = Depends(current_tenant)):
    async with request.app.state.pool.connection() as conn, AsyncTenantSession(
        conn, tenant_id
    ) as s:
        evaluations = await AsyncEvaluationRepo(s).list(lead_id)
    if not evaluations:
        raise HTTPException(404, "lead not found")
    return [strip_tenant(e.model_dump(mode="json")) for e in evaluations]
