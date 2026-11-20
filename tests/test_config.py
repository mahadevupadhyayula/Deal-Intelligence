"""Gate 1 unit tests for the settings loader.

WHAT: Checks required-variable enforcement and secret redaction.
WHERE: Verifies `agents/config.py`.
WHY: Misconfiguration must fail at startup; secrets must never leak via repr.
"""
import pytest
from pydantic import ValidationError

from agents.config import Settings


def test_missing_database_url_fails(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_secrets_not_in_repr(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2@h/db")
    s = Settings(_env_file=None)
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(s)
