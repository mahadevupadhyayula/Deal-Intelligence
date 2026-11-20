"""Minimal SQL migration runner.

WHAT: Applies `migrations/*.sql` in filename order, each in its own transaction,
recording versions in `schema_migrations`. Run with `python -m agents.migrate`.
WHERE: Runs as the ADMIN role (owns tables, creates roles/extensions) before the
app starts; the app itself connects as `app_user` and never runs DDL.
WHY: Plain SQL keeps RLS/pgvector DDL explicit and reviewable; a ~50-line runner
avoids an ORM/Alembic dependency we don't otherwise need. An advisory lock makes
concurrent runs (e.g. two pods starting) safe.
"""
from pathlib import Path

import psycopg
from psycopg import sql

from agents.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_LOCK_ID = 727_002  # arbitrary constant shared by all runners


def migrate(admin_url: str, app_user_password: str | None = None) -> list[str]:
    """Apply pending migrations; return the versions applied this run."""
    applied_now: list[str] = []
    with psycopg.connect(admin_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            # Whole file + version record commit together, or not at all.
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            applied_now.append(path.stem)
        # Password is env-supplied (not in SQL) and cluster-level, so re-applying is harmless.
        if app_user_password:
            conn.execute(
                sql.SQL("ALTER ROLE app_user PASSWORD {}").format(sql.Literal(app_user_password))
            )
        conn.commit()
    return applied_now


def main() -> None:
    s = get_settings()
    if s.admin_database_url is None:
        raise SystemExit("ADMIN_DATABASE_URL is required to run migrations")
    pw = s.app_user_password.get_secret_value() if s.app_user_password else None
    applied = migrate(s.admin_database_url.get_secret_value(), pw)
    print(f"applied: {applied}" if applied else "up to date")


if __name__ == "__main__":
    main()
