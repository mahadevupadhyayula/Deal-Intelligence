"""Shared pytest fixtures: migrated test DB, app_user connection with per-test rollback.

WHAT: Session fixtures that migrate the test database (as admin) and seed two tenants,
plus a function-scoped `db` fixture (as `app_user`) that rolls back after every test.
WHERE: Used by all tests that touch PostgreSQL (Gate 1).
WHY: Tests connect as `app_user`, NOT a superuser, because superusers bypass row-level
security and would make isolation tests meaningless. Rollback (vs. truncate) is fast and
isolates tests. We never fall back to DATABASE_URL: tests must not touch the primary DB.
DB tests skip, not fail, when no database is configured so pure-Pydantic tests run anywhere.
"""
import psycopg
import pytest
from pydantic import ValidationError

from agents.config import get_settings
from agents.db import get_connection
from agents.migrate import migrate

TENANT_A, TENANT_B = "tenant-a", "tenant-b"


@pytest.fixture(scope="session")
def migrated_test_db():
    """Migrate the test DB as admin and seed tenants (reference data, committed once)."""
    try:
        s = get_settings()
        if s.test_database_url is None or s.test_admin_database_url is None:
            pytest.skip("TEST_DATABASE_URL / TEST_ADMIN_DATABASE_URL not set")
        admin_url = s.test_admin_database_url.get_secret_value()
        pw = s.app_user_password.get_secret_value() if s.app_user_password else None
        migrate(admin_url, pw)
        with psycopg.connect(admin_url) as admin:
            for t in (TENANT_A, TENANT_B):
                admin.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (t, t))
            admin.commit()
    except (ValidationError, psycopg.Error) as exc:  # bad env or unreachable server
        pytest.skip(f"test database unavailable: {exc}")
    yield s.test_database_url.get_secret_value()
    with psycopg.connect(admin_url) as admin:
        admin.execute("DELETE FROM tenants WHERE tenant_id IN (%s, %s)", (TENANT_A, TENANT_B))


@pytest.fixture(scope="session")
def _db_conn(migrated_test_db):
    conn = get_connection(migrated_test_db)
    yield conn
    conn.close()


@pytest.fixture
def db(_db_conn):
    """app_user connection inside a transaction that is always rolled back."""
    _db_conn.rollback()
    # Open the outer transaction now so TenantSession uses savepoints and can never
    # commit test data.
    _db_conn.execute("SELECT 1")
    yield _db_conn
    _db_conn.rollback()


@pytest.fixture
def tenant_table(db):
    """Throwaway tenant-scoped table (TEMP + rolled back, never persists)."""
    with db.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE notes (id serial PRIMARY KEY, tenant_id text NOT NULL, body text)"
        )
    return "notes"
