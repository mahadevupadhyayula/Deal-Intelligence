"""Structured JSON logging with per-job context and hashed tenant ids.

WHAT: `JsonFormatter`, `configure_logging(service)`, `bind_context`/`clear_context`,
`hash_tenant`, and `log_event` (a log line with structured fields).
WHERE: Called once at API and worker start; `run_job` and the HTTP middleware bind context.
WHY: Context lives in a `ContextVar`, so every line emitted underneath (including from
libraries) carries `job_id`/`kind`/`tenant` without threading arguments through call sites, and
concurrent asyncio tasks cannot leak context into each other (each task has its own copy).
Standard-library logging is kept (no structlog): uvicorn, arq and LangChain already use it, so
one system formats everything. Redaction (Spec 006) runs first via the log-record factory, and
the formatter redacts exception text again because tracebacks are formatted after that point.
Tenants are logged as a keyed hash: stable for correlation, not reversible from the logs. Keys
differ per process only when LOG_HASH_KEY is unset, which is why compose requires it (the API and
worker must hash identically for cross-service correlation).
"""
import hashlib
import hmac
import json
import logging
import secrets
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

from agents.guardrails.redact import redact

_ctx: ContextVar[dict] = ContextVar("di_log_context", default={})  # noqa: B039 - never mutated
_STD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
_MAX_EXC = 4000
_fallback_key: bytes | None = None


def bind_context(**fields) -> None:
    """Add fields to this task's log context (copy-on-write, never mutates a shared dict)."""
    _ctx.set({**_ctx.get(), **{k: v for k, v in fields.items() if v is not None}})


def clear_context() -> None:
    _ctx.set({})


def get_context() -> dict:
    return dict(_ctx.get())


def hash_tenant(tenant_id: str, key: str | bytes | None = None) -> str:
    """HMAC-SHA256(key, tenant_id)[:12]. Falls back to a random per-process key with one warning."""
    global _fallback_key
    if key is None:
        from agents.config import get_settings

        secret = get_settings().log_hash_key
        key = secret.get_secret_value() if secret is not None else None
    if not key:
        if _fallback_key is None:
            _fallback_key = secrets.token_bytes(32)
            logging.getLogger(__name__).warning(
                "LOG_HASH_KEY is not set: tenant hashes differ between processes")
        key = _fallback_key
    raw = key if isinstance(key, bytes) else key.encode()
    return hmac.new(raw, tenant_id.encode(), hashlib.sha256).hexdigest()[:12]


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """One structured line: `event` is the message, `fields` are the event's data."""
    logger.log(level, event, extra={"di_fields": fields})


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname, "service": self.service, "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        out.update(get_context())
        out.update(getattr(record, "di_fields", {}))
        if record.exc_info:
            # Type + redacted, truncated traceback text only: no local variables.
            out["exc"] = redact(self.formatException(record.exc_info))[:_MAX_EXC]
        return json.dumps(out, default=str)


_OWNED = "_di_handler"
# Libraries that install their own handlers: route them through ours instead.
# `uvicorn.access` is deliberately NOT routed: the API runs with --no-access-log (our middleware
# writes the structured access line) and re-enabling propagation would resurrect probe noise.
_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "arq", "arq.worker", "arq.jobs",
                    "arq.connections")


def configure_logging(service: str, fmt: str | None = None, level: str | None = None) -> None:
    """Idempotent: replaces only the handler this function installed earlier."""
    from agents.config import get_settings

    s = get_settings()
    fmt, level = fmt or s.log_format, level or s.log_level
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _OWNED, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _OWNED, True)
    handler.setFormatter(
        JsonFormatter(service) if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _LIBRARY_LOGGERS:
        lib = logging.getLogger(name)
        lib.handlers.clear()
        lib.propagate = True
