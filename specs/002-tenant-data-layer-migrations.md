# Spec 002 — Tenant-Scoped Data Layer & Migrations

**Status:** Done
**Phase:** 1 · **Depends on:** 001

## Objective
Create the first real tables and a repository layer where a query without a `tenant_id` is impossible to write, backed by Postgres row-level security (RLS) as defense in depth. After this spec, API and agent code persist and load `LeadProfile`, `DealState` (with conflicts), and `HandoffBrief` only through tenant-bound repositories.

## Technical Requirements

### 1. Migrations — `migrations/` + `agents/migrate.py`
- Plain versioned SQL files (`0001_init.sql`, …) and a small runner: applies unapplied files in order, each in a transaction, records them in `schema_migrations(version, applied_at)`. Idempotent; safe to run twice.
- **Decision for review:** plain SQL + tiny runner instead of Alembic. We have no ORM, and raw SQL keeps RLS/pgvector DDL explicit. Revisit if migration needs grow.

### 2. Schema (`0001_init.sql`)
- `tenants(tenant_id text PK, name, created_at)`
- `leads`, `deals`, `conflicts`, `handoff_briefs`: each has `tenant_id text NOT NULL REFERENCES tenants`, a natural id (`lead_id`, `deal_id`, `conflict_id`), and a `data jsonb` column holding the validated Pydantic JSON plus the few columns we filter on (`stage`, `status`, `risk`, `deal_id`).
- Primary keys and foreign keys are **composite with `tenant_id`** (e.g. `PRIMARY KEY (tenant_id, deal_id)`, conflicts reference `(tenant_id, deal_id)`), so a cross-tenant reference is rejected by the database itself.
- Indexes lead with `tenant_id`.
- Wiki/vector tables are deferred to Spec 005.

### 3. Row-level security (`0002_rls.sql`)
- `ENABLE` + `FORCE ROW LEVEL SECURITY` on every tenant table; policy: `tenant_id = current_setting('app.tenant_id', true)`.
- Non-superuser role `app_user` (no `BYPASSRLS`) used by the API/workers. Superusers bypass RLS, so tests and the app must connect as `app_user`; migrations run as the owner/admin role.
- Unset `app.tenant_id` ⇒ zero rows visible and writes rejected (fail closed).

### 4. Repository layer — `agents/repos/`
- `TenantSession(conn, tenant_id)` context manager: opens a transaction and runs `SELECT set_config('app.tenant_id', %s, true)` (transaction-local, so pooled connections can't leak a tenant).
- Repositories (`LeadRepo`, `DealRepo`, `ConflictRepo`, `HandoffRepo`) take a `TenantSession` only; methods: `upsert`, `get`, `list`. Every SQL statement also includes an explicit `WHERE tenant_id = %s` (belt and braces with RLS).
- Writing a model whose `tenant_id` differs from the session's raises `TenantMismatchError` before touching the DB.
- `DealRepo.save` persists the deal and its conflicts atomically; `get` reassembles `DealState`.

### 5. Config / env
- `.env.example`: add `ADMIN_DATABASE_URL` (migrations) and keep `DATABASE_URL` as `app_user`. `agents/config.py` gains `admin_database_url`.

### 6. Docs
- WHAT/WHERE/WHY docstring in every new file and an explanatory comment header in each SQL file.

## Out of Scope
- Wiki/vector tables and tenant-filtered similarity search (Spec 005)
- `PostgresSaver` checkpoint tables (Spec 007)
- Connection pooling, API wiring (Spec 003)
- OAuth token vault tables (Spec 009), webhook idempotency table (Spec 010)

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [x] Migration runner applies on an empty DB, is idempotent on re-run, and records versions
- [x] Round-trip: each model saved and loaded through its repo equals the original
- [x] **Isolation:** with tenants A and B seeded, A's session cannot `get`/`list` B's rows, even via crafted ids
- [x] **RLS fail-closed:** raw SQL as `app_user` with `app.tenant_id` unset returns 0 rows and INSERT fails
- [x] **RLS backstop:** raw `SELECT *` without a `WHERE` under tenant A's session returns only A's rows
- [x] Cross-tenant FK: inserting a conflict for tenant A pointing at tenant B's deal fails
- [x] `TenantMismatchError` raised when a model's `tenant_id` ≠ session's
- [x] Setting is transaction-local: after commit, a reused connection sees no tenant
- [x] Tests run as `app_user` and roll back via the Spec 001 fixtures

### Gate 2 — Agent Behavior
- [x] **N/A** — no agents in this spec

### Gate 3 — Live Smoke Test
- [x] `python -m agents.migrate` against a fresh database, then `psql "$ADMIN_DATABASE_URL" -c "\dt"` lists `tenants, leads, deals, conflicts, handoff_briefs, schema_migrations`
- [x] `psql "$ADMIN_DATABASE_URL" -c "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('leads','deals','conflicts','handoff_briefs');"` shows `t | t` for all four
- [x] As `app_user` via `psql`: `SELECT count(*) FROM deals;` with no tenant set returns 0 while the admin role sees seeded rows

### Definition of Done
- [x] All gates pass; Verification Report (Pass/Fail) output
- [x] Docstring rule satisfied in every new file
- [x] `docs/build-logs/YYYY-MM-DD-day-XX.md` written; README progress table updated (Spec 002 → Done)

## Open Questions for Review
1. Plain SQL runner vs Alembic (recommended: plain SQL).
2. Store models as `jsonb` + a few filter columns (recommended, schemas will evolve) vs fully normalized columns.
