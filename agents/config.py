"""Environment-driven settings loader.

WHAT: Typed `Settings` read from environment / `.env`, with a cached accessor.
WHERE: Imported by `agents/db.py`, the API, and workers; the only place that
reads configuration.
WHY: Zero hardcoded secrets. Validating at startup makes a missing variable a
clear immediate error rather than a mid-request failure. `SecretStr` keeps
secrets out of `repr`/logs.
"""
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
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
    # Deployment-test knob for the free `echo` job (Spec 008): 0 disables the delay entirely.
    echo_max_delay_seconds: float = 0

    # --- Shared guardrails (Spec 006) ---
    rate_limit_jobs_per_minute: int = 30
    tenant_max_active_jobs: int = 10  # queued + running
    tenant_daily_max_llm_calls: int = 500  # per tenant per UTC day
    tenant_daily_max_searches: int = 500
    trace_redaction: bool = True
    injection_scan_enabled: bool = True

    # --- Observability (Spec 009) ---
    log_format: str = "text"  # "json" in containers
    log_level: str = "INFO"
    log_hash_key: SecretStr | None = None  # keys the tenant hash in logs/traces; share across services
    metrics_enabled: bool = True
    metrics_bind_host: str = "127.0.0.1"  # compose sets 0.0.0.0 (internal network only)
    worker_metrics_port: int = 9100
    api_metrics_port: int = 9101
    # Flat per-call prices for the cost ESTIMATE; EVAL_PRICE_* are read as a fallback.
    price_per_llm_call: float = Field(0.01, validation_alias=AliasChoices(
        "PRICE_PER_LLM_CALL", "EVAL_PRICE_PER_LLM_CALL"))
    price_per_search: float = Field(0.008, validation_alias=AliasChoices(
        "PRICE_PER_SEARCH", "EVAL_PRICE_PER_SEARCH"))

    # --- Stale-job reaper (Spec 010) ---
    reaper_enabled: bool = True
    reaper_interval_seconds: float = 60
    reaper_max_age_seconds: float | None = None  # None -> job_timeout_seconds + 120

    # --- Eval harness (Spec 007) ---
    eval_default_repeats: int = 3
    eval_concurrency: int = 2
    eval_run_timeout_seconds: float = 120
    eval_max_runs: int = 200  # hard spend cap per CLI invocation
    eval_report_dir: str = "evals/reports"
    # Cost estimate = calls x price. LangChain adapters do not surface token counts here, so a
    # flat per-call price is the honest, configurable approximation.
    eval_price_per_llm_call: float = 0.01
    eval_price_per_search: float = 0.008


@lru_cache
def get_settings() -> Settings:
    # Cached: env is read once per process; tests call cache_clear() to override.
    return Settings()
