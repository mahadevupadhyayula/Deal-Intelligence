"""Names of valid job kinds.

WHAT: A frozen set of kind strings the API accepts.
WHERE: Imported by `api/routes/jobs.py`; handlers for these kinds live in `workers/handlers.py`.
WHY: The API process must not import LangGraph/LangChain just to validate a string. Keeping
the names here (and the heavy handlers in the worker package) keeps API images small, start-up
fast and its dependency surface narrow. Adding a kind means adding it here AND in the worker.
"""

JOB_KINDS: frozenset[str] = frozenset({"echo", "lead_research"})
