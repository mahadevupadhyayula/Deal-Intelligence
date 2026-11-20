"""Route modules for the HTTP API.

WHAT: Namespace for the web layer.
WHERE: Entry point for clients/UI; depends on `agents/` for schemas and persistence.
WHY: The API only validates, records and enqueues; all agent work lives in `workers/`.
"""
