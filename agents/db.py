"""PostgreSQL connection helpers and the pgvector availability check.

WHAT: Opens psycopg connections and verifies the `vector` extension.
WHERE: Lowest data-access layer; Spec 002 builds the tenant-scoped data layer
on top. Used by tests and the Gate 3 smoke test.
WHY: psycopg v3 is the driver PostgresSaver uses, so we avoid two drivers. No
ORM yet: schemas/migrations are deferred to Spec 002.
"""
import psycopg


def get_connection(url: str) -> psycopg.Connection:
    # autocommit off (default): callers/fixtures control transaction boundaries,
    # which is what lets tests roll back.
    return psycopg.connect(url)


def check_pgvector(conn: psycopg.Connection) -> bool:
    """Ensure the pgvector extension exists; raise if it cannot be enabled."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        if cur.fetchone() is None:
            raise RuntimeError("pgvector extension is not installed")
    conn.commit()
    return True
