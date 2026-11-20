# Spec 001 — Foundation: Project Config, Core Schemas, Database

**Status:** Done
**Phase:** 1 · **Depends on:** none

## Objective
Establish the Python project, a typed environment-driven config loader, the four core Pydantic v2 models every agent will exchange, and a verified local PostgreSQL + `pgvector` connection with a transactional test harness. Every later spec builds on these; `tenant_id` enforcement starts here.

## Technical Requirements

### 1. `pyproject.toml`
- Python `>=3.11`; package discovery for `agents`, `api`, `workers`.
- Runtime deps: `pydantic>=2`, `pydantic-settings`, `psycopg[binary]` (v3), `pgvector`.
- Dev deps: `pytest`, `ruff`. (LangGraph/FastAPI/ARQ deps are added by the specs that use them.)
- `[tool.pytest.ini_options]`: `testpaths = ["tests"]`.

### 2. Config loader — `agents/config.py`
- `Settings(BaseSettings)` reading `.env`: `app_env`, `database_url`, `test_database_url`, `redis_url`.
- Secrets typed as `SecretStr`; no defaults for secrets; cached accessor `get_settings()`.
- Missing required values raise a clear validation error at startup.

### 3. Core models — `agents/schemas/`
All models inherit a `TenantModel` base in `agents/schemas/base.py` with required `tenant_id: str` (non-empty, validated), `extra="forbid"`, and frozen where sensible.
- `LeadProfile`: `lead_id`, `company_name`, `domain`, `industry`, `employee_count`, `signals: list[str]`, `sources: list[str]` (HttpUrl-like), `confidence: float` (0–1).
- `ConflictItem`: `conflict_id`, `deal_id`, `field`, `crm_value`, `evidence_value`, `evidence_source`, `risk: Literal["low","high"]`, `status: Literal["pending","approved","rejected","auto_applied"]`.
- `DealState`: `deal_id`, `stage`, `amount`, `lead: LeadProfile | None`, `conflicts: list[ConflictItem]`, `updated_at`. Nested models must share the parent's `tenant_id` (model validator rejects mismatch).
- `HandoffBrief`: `deal_id`, `summary`, `commitments`, `stakeholders`, `risks`, `open_questions`, `generated_at`.

### 4. Database — `agents/db.py`
- `get_connection(url)` returning a psycopg connection; `check_pgvector(conn)` verifying `CREATE EXTENSION IF NOT EXISTS vector` succeeds and `SELECT extname FROM pg_extension` lists `vector`.
- No ORM or migrations yet (Spec 002).

### 5. Test harness — `tests/conftest.py`
- Session fixture connecting to `test_database_url` (skips with clear message if unreachable).
- Function-scoped `db` fixture: opens a transaction, yields the connection, **rolls back** after each test.
- Fixture to create a throwaway tenant-scoped table (`tenant_id` column) used by isolation tests.

### 6. Docs
- Every file carries the WHAT/WHERE/WHY docstring (CLAUDE.md §c).

## Out of Scope
- Migrations / real table schemas and row-level security (Spec 002)
- FastAPI, ARQ, LangGraph, LLM calls (Specs 003+)
- Docker compose (Spec 015)

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [x] Each model rejects missing/empty `tenant_id`, unknown fields, and out-of-range values (`confidence`, `risk`)
- [x] `DealState` rejects nested `LeadProfile`/`ConflictItem` with a different `tenant_id`
- [x] Models round-trip through `model_dump_json` / `model_validate_json`
- [x] `Settings` fails loudly when `DATABASE_URL` is missing; secrets do not appear in `repr`
- [x] DB isolation test: rows inserted for tenant A are never returned by a tenant-B-filtered query
- [x] Rollback fixture: data written in one test is absent in the next

### Gate 2 — Agent Behavior
- [x] **N/A** — no LangGraph agents in this spec

### Gate 3 — Live Smoke Test
- [x] `psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extname FROM pg_extension WHERE extname='vector';"` returns one row
- [x] `python -c "from agents.config import get_settings; from agents.db import get_connection, check_pgvector; check_pgvector(get_connection(get_settings().database_url))"` exits 0

### Definition of Done
- [x] All gates pass; Verification Report (Pass/Fail) output
- [x] Docstring rule satisfied in every new file
- [x] `docs/build-logs/YYYY-MM-DD-day-01.md` written; README progress table updated (Spec 001 → Done)
