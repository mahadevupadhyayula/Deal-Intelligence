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


@lru_cache
def get_settings() -> Settings:
    # Cached: env is read once per process; tests call cache_clear() to override.
    return Settings()
