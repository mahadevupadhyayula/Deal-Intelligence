"""Bounded retry for transient external errors.

WHAT: `call_with_retries` re-runs an async call on errors flagged `transient`, with
exponential backoff, and reports how many calls were made.
WHERE: Used by graph nodes around every search and LLM call.
WHY: One 429/timeout should not fail a whole research job, but retries cost money, so the
caller gets the real call count (to charge the budget) and a cap on remaining budget is
honoured. Retry lives in the node layer, not the adapters, because the per-run budget counters
are per-run state, while adapters are shared across jobs.
"""
import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def call_with_retries(
    fn: Callable[[], Awaitable[T]], *, attempts: int, backoff: float,
    max_calls: int | None = None, sleep=asyncio.sleep,
) -> tuple[T, int]:
    """Return (result, calls_made). On failure re-raise with `.calls_made` set on the error."""
    calls = 0
    while True:
        calls += 1
        try:
            return await fn(), calls
        except Exception as exc:
            transient = bool(getattr(exc, "transient", False))
            out_of_retries = calls > attempts
            out_of_budget = max_calls is not None and calls >= max_calls
            if not transient or out_of_retries or out_of_budget:
                exc.calls_made = calls  # type: ignore[attr-defined]
                raise
            await sleep(backoff * (2 ** (calls - 1)))
