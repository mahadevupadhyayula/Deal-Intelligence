"""Gate 1 tests for the migration runner.

WHAT: Applies migrations to a brand-new scratch database, re-runs them, checks results.
WHERE: Verifies `agents/migrate.py` and `migrations/*.sql`.
WHY: A fresh DB is the real-world first deploy; idempotency protects repeated pod starts.
"""
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agents.config import get_settings
from agents.migrate import MIGRATIONS_DIR, migrate

SCRATCH = "deal_intel_migtest"


@pytest.fixture
def scratch_url(migrated_test_db):
    admin_url = get_settings().test_admin_database_url.get_secret_value()
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
        c.execute(f"CREATE DATABASE {SCRATCH}")
    yield make_conninfo(admin_url, dbname=SCRATCH)
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")


def test_migrate_fresh_db_and_idempotent(scratch_url):
    expected = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    assert migrate(scratch_url) == expected
    assert migrate(scratch_url) == []  # second run is a no-op
    with psycopg.connect(scratch_url) as c:
        versions = [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        tables = {r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        rls = c.execute(
            "SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class "
            "WHERE relname IN ('leads','deals','conflicts','handoff_briefs')").fetchone()[0]
    assert versions == expected
    assert {"tenants", "leads", "deals", "conflicts", "handoff_briefs"} <= tables
    assert rls is True
    assert conninfo_to_dict(scratch_url)["dbname"] == SCRATCH
