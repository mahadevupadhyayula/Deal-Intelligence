"""Secret and PII redaction for logs, stored errors and traces.

WHAT: `redact` (secret shapes + registered live secrets), `redact_pii` (emails/phones),
`install_log_redaction` (apply `redact` to every log record).
WHERE: Worker (`run_job` error text, startup), API startup, LangSmith anonymiser.
WHY: Provider error messages and tracebacks can echo fragments of API keys; scraped text can
contain contact details. Redaction runs where data LEAVES our control (logs, stored error text,
traces). It is NOT applied to research data stored for the tenant or to what the model reads,
which would corrupt evidence. Known secret VALUES are matched literally in addition to
generic key shapes, because shapes alone miss unfamiliar formats.
"""
import logging
import re

_SECRET_SHAPES = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_*.-]{4,}"),
    re.compile(r"\btvly-[A-Za-z0-9_*.-]{4,}"),
    re.compile(r"\blsv2_[A-Za-z0-9_*.-]{4,}"),
]
_DB_URL = re.compile(r"(\b\w+://[^:/\s@]+:)[^@\s]+(@)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+\d[\d\s().-]{7,}\d|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})")

_LIVE: set[str] = set()
_installed = False


def register_secret(value: str | None) -> None:
    """Remember a live secret value so it is redacted wherever it appears."""
    if value and len(value) >= 8:
        _LIVE.add(value)


def redact(text: str) -> str:
    for value in _LIVE:
        text = text.replace(value, "[REDACTED]")
    text = _DB_URL.sub(r"\1[REDACTED]\2", text)
    for rx in _SECRET_SHAPES:
        text = rx.sub("[REDACTED]", text)
    return text


def redact_pii(text: str) -> str:
    return _PHONE.sub("[PHONE]", _EMAIL.sub("[EMAIL]", text))


def install_log_redaction() -> None:
    """Wrap the log-record factory so every record's message is redacted (idempotent)."""
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        record.msg = redact(record.getMessage())
        record.args = None
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
