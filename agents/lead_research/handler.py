"""`lead_research` job handler.

WHAT: Validates the payload, runs the research graph for one lead, returns the result dict.
WHERE: Registered in `workers/handlers.py`; invoked by `workers/tasks.py:run_job`.
WHY: The handler is thin: payload validation, wiring, and LangSmith metadata. The tenant id in
traces is hashed (opaque, still joinable via job_id in our DB). A missing search/LLM
configuration fails the job with a clear message instead of crashing the worker at startup.
"""
import hashlib
from typing import Any

from agents.config import get_settings
from agents.jobs import JobContext
from agents.lead_research.config import ResearchConfig
from agents.lead_research.graph import build_graph
from agents.lead_research.models import LeadResearchInput
from agents.lead_research.nodes import ResearchDeps
from agents.threads import make_thread_id


async def lead_research(
    ctx: JobContext, tenant_id: str, job_id: str, payload: dict[str, Any],
    cfg: ResearchConfig | None = None,
) -> dict[str, Any]:
    if ctx.search is None or ctx.llm is None:
        raise RuntimeError("lead research is not configured (set TAVILY_API_KEY and LLM_MODEL)")
    inp = LeadResearchInput.model_validate(payload)
    cfg = cfg or ResearchConfig.from_settings(get_settings())
    graph = build_graph(ResearchDeps(pool=ctx.pool, search=ctx.search, llm=ctx.llm, cfg=cfg))
    config = {
        "run_name": "lead_research",
        "tags": ["lead_research"],
        "metadata": {  # no secrets; tenant id hashed
            "tenant_hash": hashlib.sha256(tenant_id.encode()).hexdigest()[:12],
            "job_id": job_id, "lead_id": inp.lead_id,
        },
        "configurable": {"thread_id": make_thread_id(tenant_id, inp.lead_id, job_id)},
    }
    final = await graph.ainvoke(
        {"tenant_id": tenant_id, "lead_id": inp.lead_id, "job_id": job_id,
         "company_name": inp.company_name, "domain": inp.domain, "trace": []}, config)
    evaluation = final["evaluation"]
    return {
        "status": evaluation.status,
        "profile": final["profile"].model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
    }
