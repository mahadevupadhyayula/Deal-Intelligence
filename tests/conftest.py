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


# --- Async / API fixtures (Spec 003) -------------------------------------------------------
# Pool-based tests really COMMIT (pools hand out separate connections), so instead of rollback
# we delete the rows they created, as admin, after each test.
import psycopg_pool

from agents.security import generate_api_key, hash_api_key


@pytest.fixture
def admin_url(migrated_test_db):
    return get_settings().test_admin_database_url.get_secret_value()


@pytest.fixture
def clean_tenant_rows(admin_url):
    def wipe():
        with psycopg.connect(admin_url) as c:
            c.execute("DELETE FROM jobs WHERE tenant_id IN (%s, %s)", (TENANT_A, TENANT_B))
            c.execute("DELETE FROM icp_segments WHERE tenant_id IN (%s, %s)", (TENANT_A, TENANT_B))
            c.execute("DELETE FROM api_keys WHERE tenant_id IN (%s, %s)", (TENANT_A, TENANT_B))
    wipe()
    yield
    wipe()


@pytest.fixture
async def apool(migrated_test_db, clean_tenant_rows):
    pool = psycopg_pool.AsyncConnectionPool(migrated_test_db, open=False)
    await pool.open()
    yield pool
    await pool.close()


@pytest.fixture
async def adb(migrated_test_db):
    """Async app_user connection in a rolled-back transaction."""
    conn = await psycopg.AsyncConnection.connect(migrated_test_db)
    await conn.execute("SELECT 1")
    yield conn
    await conn.rollback()
    await conn.close()


@pytest.fixture
def api_keys(admin_url, clean_tenant_rows):
    """One live key per tenant plus one revoked key for tenant A."""
    keys = {TENANT_A: generate_api_key(), TENANT_B: generate_api_key(), "revoked": generate_api_key()}
    with psycopg.connect(admin_url) as c:
        for tenant, key in ((TENANT_A, keys[TENANT_A]), (TENANT_B, keys[TENANT_B])):
            c.execute("INSERT INTO api_keys (key_hash, tenant_id) VALUES (%s, %s)",
                      (hash_api_key(key), tenant))
        c.execute("INSERT INTO api_keys (key_hash, tenant_id, revoked_at) VALUES (%s, %s, now())",
                  (hash_api_key(keys["revoked"]), TENANT_A))
    return keys
