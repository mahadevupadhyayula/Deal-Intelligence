"""Environment-driven settings loader.

WHAT: Typed `Settings` read from environment / `.env`, with a cached accessor.
WHERE: Imported by `agents/db.py`, the API, and workers; the only place that
reads configuration.
WHY: Zero hardcoded secrets. Validating at startup makes a missing variable a
clear immediate error rather than a mid-request failure. `SecretStr` keeps
secrets out of `repr`/logs.
"""
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    # Passwords live inside the URL, so treat the whole URL as secret.
    database_url: SecretStr  # required: no default on purpose
    test_database_url: SecretStr | None = None
    # Admin role: runs migrations / seeds tenants. The app never uses it.
    admin_database_url: SecretStr | None = None
    test_admin_database_url: SecretStr | None = None
    app_user_password: SecretStr | None = None
    redis_url: str = "redis://localhost:6379/0"
    job_payload_max_bytes: int = 65536

    # --- Lead research (Spec 005) ---
    llm_model: str | None = None  # "<provider>:<model>", e.g. anthropic:claude-sonnet-5-5
    tavily_api_key: SecretStr | None = None
    research_max_searches: int = 8
    research_max_llm_calls: int = 8  # retries included
    research_max_refine_loops: int = 1
    research_max_sources: int = 8
    research_source_char_limit: int = 6000
    research_max_total_chars: int = 40000
    research_retry_attempts: int = 2
    research_retry_backoff_seconds: float = 1.0
    research_run_timeout_seconds: float = 270  # inner limit, below ARQ's
    job_timeout_seconds: int = 300  # ARQ's outer limit

    # --- Shared guardrails (Spec 006) ---
    rate_limit_jobs_per_minute: int = 30
    tenant_max_active_jobs: int = 10  # queued + running
    tenant_daily_max_llm_calls: int = 500  # per tenant per UTC day
    tenant_daily_max_searches: int = 500
    trace_redaction: bool = True
    injection_scan_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    # Cached: env is read once per process; tests call cache_clear() to override.
    return Settings()
