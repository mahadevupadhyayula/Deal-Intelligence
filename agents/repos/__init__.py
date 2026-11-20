"""Tenant-scoped repositories: the only sanctioned path to the database.

WHAT: Re-exports `TenantSession` and the four repositories.
WHERE: Sits between agents/API/workers (callers) and PostgreSQL (see migrations/).
WHY: Funnelling all persistence through repositories that require a TenantSession
makes "forgot the tenant filter" structurally impossible at the application layer;
RLS covers the rest.
"""
from agents.repos.deal import ConflictRepo, DealRepo
from agents.repos.handoff import HandoffRepo
from agents.repos.lead import LeadRepo
from agents.repos.session import TenantMismatchError, TenantSession

__all__ = [
    "ConflictRepo", "DealRepo", "HandoffRepo", "LeadRepo", "TenantMismatchError", "TenantSession",
]
