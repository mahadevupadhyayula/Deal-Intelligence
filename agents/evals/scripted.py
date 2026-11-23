"""Scripted (replay) LLM double shared by tests and the eval harness.

WHAT: `ScriptedLLM`, a `StructuredLLM` that replays recorded structured outputs in order and
records every prompt it received.
WHERE: Golden tests (`tests/`) and `scripted` mode of the eval runner.
WHY: Deterministic and free. It is strict: an unexpected extra call fails loudly, so call
counts are verified rather than assumed. It lives here (not in `tests/`) so the eval package
never imports test code.
"""
import asyncio

from agents.lead_research.llm import LLMError
from agents.lead_research.models import QueryPlan, ResearchFindings


class ScriptedLLM:
    def __init__(self, plans=(), synths=(), delay: float = 0.0):
        self.plans, self.synths, self.delay = list(plans), list(synths), delay
        self.calls: list[tuple[str, str]] = []  # (schema name, user prompt)

    async def generate(self, schema, system, user):
        self.calls.append((schema.__name__, user))
        if self.delay:
            await asyncio.sleep(self.delay)
        queue = self.plans if schema is QueryPlan else self.synths if schema is ResearchFindings else None
        assert queue is not None, f"unexpected schema {schema}"
        assert queue, f"unexpected extra {schema.__name__} call (script exhausted)"
        item = queue.pop(0)
        if isinstance(item, dict) and "error" in item:
            raise LLMError("scripted failure", transient=item["error"] == "transient")
        return schema.model_validate(item)
