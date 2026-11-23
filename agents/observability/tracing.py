"""Run metadata that links a LangSmith trace to its job.

WHAT: `run_metadata(job_id, kind, tenant_id)` -> (tags, metadata); `tracing_enabled()`.
WHERE: Merged into the LangGraph run config in `agents/lead_research/handler.py`.
WHY: A trace is only useful if you can reach it from a job id and back. The tenant appears
only as the keyed hash (same as logs), never raw. Content redaction is separate and unchanged
(`agents/guardrails/tracing.py`).
"""
import os

from agents.config import get_settings
from agents.observability.logging import hash_tenant


def tracing_enabled() -> bool:
    return any(os.environ.get(v, "").lower() == "true"
               for v in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"))


def run_metadata(job_id: str, kind: str, tenant_id: str) -> tuple[list[str], dict]:
    return [kind, f"job:{job_id}"], {
        "job_id": job_id, "kind": kind, "tenant_hash": hash_tenant(tenant_id),
        "app_env": get_settings().app_env,
    }
