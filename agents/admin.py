"""Admin CLI: provision tenants and API keys.

WHAT: `python -m agents.admin create-tenant <id>` and `create-api-key <tenant_id>`.
WHERE: Operator tool, run with the ADMIN role (tenants/keys are not app-writable).
WHY: Keeping provisioning out of the app role means a compromised API process cannot mint
credentials. The plaintext key is printed once and never stored or logged.
"""
import sys

import psycopg

from agents.config import get_settings
from agents.security import generate_api_key, hash_api_key


def _admin_url() -> str:
    s = get_settings()
    if s.admin_database_url is None:
        raise SystemExit("ADMIN_DATABASE_URL is required")
    return s.admin_database_url.get_secret_value()


def create_tenant(tenant_id: str, name: str | None = None) -> None:
    with psycopg.connect(_admin_url()) as conn:
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, name or tenant_id),
        )


def create_api_key(tenant_id: str) -> str:
    key = generate_api_key()
    with psycopg.connect(_admin_url()) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, tenant_id) VALUES (%s, %s)",
            (hash_api_key(key), tenant_id),
        )
    return key


def main(argv: list[str]) -> None:
    if len(argv) == 3 and argv[1] == "create-tenant":
        create_tenant(argv[2])
        print(f"tenant ready: {argv[2]}")
    elif len(argv) == 3 and argv[1] == "create-api-key":
        print(create_api_key(argv[2]))
    else:
        raise SystemExit("usage: python -m agents.admin (create-tenant|create-api-key) <tenant_id>")


if __name__ == "__main__":
    main(sys.argv)
