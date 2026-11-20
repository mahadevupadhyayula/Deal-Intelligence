"""Package root for all agent code.

WHAT: Namespace for config, DB helpers, shared schemas, and the four agents.
WHERE: Imported by `api/` (to validate payloads) and `workers/` (to run graphs).
WHY: One importable package keeps shared contracts (schemas) in a single place
so the API and workers can never drift apart.
"""
