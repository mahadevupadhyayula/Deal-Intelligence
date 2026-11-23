"""LangSmith trace redaction.

WHAT: `configure_trace_redaction()` installs an anonymiser so every string sent to LangSmith
passes through `redact` and `redact_pii`.
WHERE: Called at worker startup, before any traced run.
WHY: Traces contain prompts and model output, i.e. scraped company text that can include
contact details, and error text that can include key fragments. Masking at the client means
no call site has to remember to. Only trace payloads are masked: stored research data and the
text the model reads are untouched. Both the langsmith and langchain-core cached clients are
set because LangGraph/LangChain runs use the latter.
"""
from agents.guardrails.redact import redact, redact_pii


def _mask(text: str, _path) -> str:
    return redact_pii(redact(text))


def configure_trace_redaction() -> bool:
    """Returns True if the anonymiser was installed (False if langsmith is unavailable)."""
    try:
        from langchain_core.tracers import langchain as lc_tracer
        from langsmith import Client, run_trees
        from langsmith.anonymizer import create_anonymizer
    except ImportError:
        return False
    client = Client(anonymizer=create_anonymizer(_mask))
    run_trees._CLIENT = client
    lc_tracer._CLIENT = client
    return True
