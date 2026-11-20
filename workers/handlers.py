"""Worker-side registry mapping job kind -> handler.

WHAT: `JOB_HANDLERS`, the dispatch table used by `run_job`.
WHERE: Imported only by the worker process (it pulls in LangGraph/LangChain via the
lead-research agent); the API uses `agents/job_kinds.py` instead.
WHY: Keeps heavy agent dependencies out of the API process. New agents register here.
"""
from agents.lead_research.handler import lead_research

from agents.jobs import JobHandler, echo

JOB_HANDLERS: dict[str, JobHandler] = {"echo": echo, "lead_research": lead_research}
