"""Background worker package (ARQ).

WHAT: Namespace for queue consumers that execute agent jobs.
WHERE: Consumes the Redis queue the API enqueues to; scaled by KEDA in production.
WHY: Long-running LLM work must live outside the request path so FastAPI never blocks.
"""
