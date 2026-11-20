"""JobRecord: API/worker view of one queued unit of work.

WHAT: Typed row for the `jobs` table.
WHERE: Returned by `JobRepo`; serialised by the API's GET /v1/jobs/{id}.
WHY: Using a TenantModel keeps even operational records tenant-stamped and validated.
"""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from agents.schemas.base import TenantModel

JobStatus = Literal["queued", "running", "succeeded", "failed"]


class JobRecord(TenantModel):
    job_id: UUID
    kind: str
    status: JobStatus
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
