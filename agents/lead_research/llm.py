"""Structured-output LLM interface and LangChain adapter.

WHAT: `StructuredLLM` protocol (`generate(schema, system, user) -> schema instance`) and the
production adapter over LangChain's `init_chat_model(...).with_structured_output(...)`.
WHERE: Injected via `JobContext.llm`; the graph depends only on the protocol.
WHY: Provider-agnostic by design (`LLM_MODEL="<provider>:<model>"`). Tests inject a scripted
double. Provider errors are mapped to `LLMError(transient=...)` by status/name so the retry
helper retries rate limits and timeouts but not auth or schema mistakes. A malformed structured
output is retried once or twice (cheap, often fixes itself) within the budget.
"""
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_TRANSIENT_NAMES = {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError",
                    "ServiceUnavailableError", "Timeout", "ReadTimeout", "ConnectError",
                    "OverloadedError"}


class LLMError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class StructuredLLM(Protocol):
    async def generate(self, schema: type[T], system: str, user: str) -> T: ...


class LangChainLLM:
    def __init__(self, model: str):
        # Imported lazily so merely importing this module needs no provider package.
        from langchain.chat_models import init_chat_model

        self._model = init_chat_model(model)

    async def generate(self, schema: type[T], system: str, user: str) -> T:
        runnable = self._model.with_structured_output(schema)
        try:
            out = await runnable.ainvoke([("system", system), ("human", user)])
            return out if isinstance(out, schema) else schema.model_validate(out)
        except ValidationError as exc:
            raise LLMError("model returned invalid structured output", transient=True) from exc
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            transient = (type(exc).__name__ in _TRANSIENT_NAMES
                         or status == 429 or (isinstance(status, int) and status >= 500)
                         or isinstance(exc, TimeoutError))
            raise LLMError(f"LLM call failed: {type(exc).__name__}", transient=transient) from exc
