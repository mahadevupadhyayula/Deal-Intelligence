"""Lead Research agent package (Spec 005).

WHAT: Researches a company on the web and evaluates it against the tenant's ICP segments.
WHERE: Runs inside ARQ workers as the `lead_research` job (see `workers/handlers.py`); the
API never imports this package (it pulls in LangGraph/LangChain).
WHY: An explicit, bounded LangGraph pipeline (not a free-roaming agent) so every step is
testable, cost-capped, and auditable; all external services sit behind small interfaces.
"""
