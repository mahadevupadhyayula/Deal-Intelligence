"""Shared pytest fixtures: DB connection with per-test rollback.

WHAT: Session-scoped connection to the test database and a function-scoped
`db` fixture that rolls back after every test.
WHERE: Used by all tests that touch PostgreSQL (Gate 1).
WHY: Rollback (vs. truncate/recreate) is fast and guarantees isolation between
tests without touching real data. DB tests skip, not fail, when no database is
reachable so pure-Pydantic tests still run anywhere; Gate 3 proves the live DB.
"""
import psycopg
import pytest
from pydantic import ValidationError

from agents.config import get_settings
from agents.db import get_connection


@pytest.fixture(scope="session")
def _db_conn():
    try:
        s = get_settings()
        # Deliberately NO fallback to database_url: tests must never touch the
        # primary database, even though they roll back.
        if s.test_database_url is None:
            pytest.skip("TEST_DATABASE_URL not set")
        conn = get_connection(s.test_database_url.get_secret_value())
    except (ValidationError, psycopg.Error) as exc:  # bad env or unreachable server
        pytest.skip(f"test database unavailable: {exc}")
    yield conn
    conn.close()


@pytest.fixture
def db(_db_conn):
    """Connection inside a transaction that is always rolled back."""
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
