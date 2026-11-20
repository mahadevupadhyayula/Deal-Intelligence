# Spec 003 — FastAPI Skeleton + ARQ Enqueue/Worker Plumbing

**Status:** Draft
**Phase:** 1 · **Depends on:** 001, 002

## Objective
Stand up the request path every agent will use: an authenticated, tenant-aware FastAPI app that **never runs agent work inline** — it records a job, enqueues it to Redis (ARQ) and returns `202` — plus an ARQ worker that picks the job up under the correct tenant and records its outcome. The worker runs only a stub `echo` handler here; real agents plug into the same registry in Specs 004+.

## Technical Requirements

### 1. Dependencies (`pyproject.toml`)
- Runtime: `fastapi`, `uvicorn[standard]`, `arq`, `psycopg-pool`. Dev: `httpx`, `pytest-asyncio`.

### 2. Tenant authentication — `api/auth.py` + migration `0003_api_keys_jobs.sql`
- `api_keys(key_hash text PK, tenant_id text REFERENCES tenants, created_at, revoked_at)`. Keys are high-entropy random tokens; only a SHA-256 hash is stored. **Not under RLS** (it must be read *before* the tenant is known); `app_user` gets `SELECT` only.
- FastAPI dependency `current_tenant` reads `Authorization: Bearer <key>`, hashes it, resolves `tenant_id`; missing/unknown/revoked ⇒ `401`. The tenant id is **never** taken from a header, path or body.
- `python -m agents.admin create-api-key <tenant_id>` (admin role) prints a new key once. Keys are never logged.

### 3. Jobs table (same migration)
- `jobs(tenant_id, job_id uuid, kind text, status text CHECK IN ('queued','running','succeeded','failed'), payload jsonb, result jsonb, error text, created_at, updated_at, PK (tenant_id, job_id))` with composite-leading index and **forced RLS** (same policy as Spec 002); `app_user` gets `SELECT, INSERT, UPDATE`.

### 4. Async persistence — `agents/repos/async_session.py`, `agents/repos/job.py`
- `AsyncTenantSession` mirrors `TenantSession` (transaction-local `app.tenant_id`, reset on clean exit) on `psycopg.AsyncConnection`, because blocking DB calls inside `async def` handlers would stall the event loop.
- `JobRepo` (async): `create`, `get`, `mark_running`, `mark_succeeded(result)`, `mark_failed(error)`; every statement has an explicit `WHERE tenant_id`. Status transitions are validated (`queued→running→succeeded|failed` only).
- The existing sync repos stay as-is; async versions of other repos are added by the spec that needs them.

### 5. Job kinds registry — `agents/jobs.py`
- `JOB_HANDLERS: dict[str, Callable]` mapping `kind` → async handler `(tenant_id, job_id, payload) -> dict`. Ships with `echo` only. Shared by API (to reject unknown kinds) and worker (to dispatch).

### 6. Tenant-safe thread ids — `agents/threads.py`
- `make_thread_id(tenant_id, deal_id, run_id) -> "tenant:deal:run"` and `parse_thread_id`. Components are non-empty and must not contain `:`; parse round-trips. Prepares the LangGraph/`PostgresSaver` rule from CLAUDE.md §b.

### 7. API — `api/main.py`, `api/routes/`
- `POST /v1/jobs` body `{kind, payload}` → `202 {job_id, status:"queued"}`. Validates `kind` against the registry (`422`) and payload size ≤ 64 KB (`413`). Flow: insert `jobs` row → enqueue to ARQ (`_job_id=job_id`, args carry **only** `tenant_id` + `job_id`, never the payload) → return. If Redis is unreachable: mark the job `failed`, return `503`.
- `GET /v1/jobs/{job_id}` → status/result/error for the caller's tenant; another tenant's id returns `404` (no existence leak).
- `GET /healthz` (liveness, no dependencies) and `GET /readyz` (checks Postgres and Redis, `503` if either is down). Both unauthenticated.
- App lifespan owns one `AsyncConnectionPool` (as `app_user`) and one ARQ Redis pool; handlers never open ad-hoc connections. No handler performs agent/LLM work or `time.sleep`/sync I/O.

### 8. Worker — `workers/settings.py`, `workers/tasks.py`
- ARQ `WorkerSettings` with `run_job(ctx, tenant_id, job_id)`: open `AsyncTenantSession(tenant_id)` → load job (missing under RLS ⇒ abort) → `mark_running` → dispatch to handler → `mark_succeeded` / `mark_failed(str(exc))`. Failures are recorded and **not** auto-retried (`max_tries=1`); retry policy comes with real agents.
- Run with `arq workers.settings.WorkerSettings`.

### 9. Config / env
- `agents/config.py`: reuse `redis_url`; add `job_payload_max_bytes` (default 65536). `.env.example` updated.

### 10. Docs
- WHAT/WHERE/WHY docstring in every new file; SQL header comment in `0003`.

## Out of Scope
- Docker images/compose (Spec 014), K8s/KEDA (Spec 016)
- Real agent handlers (Specs 004–008); LangGraph integration
- Webhook endpoints, idempotency keys, rate limiting (Spec 010)
- CRM OAuth / token vault (Spec 009); user login/UI sessions (Spec 013)
- SQS transport (ARQ/Redis only for now); job retries, cancellation, outbox pattern
- Prometheus metrics / structured logging beyond default

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] Auth: missing, malformed, unknown and revoked keys ⇒ `401`; valid key resolves the right tenant; stored value is a hash, not the key
- [ ] `POST /v1/jobs` (ASGI client, fake enqueue): returns `202`, creates a `queued` row for the caller's tenant, enqueues with args `(tenant_id, job_id)` only
- [ ] Unknown `kind` ⇒ `422`; oversize payload ⇒ `413`; Redis failure ⇒ `503` and job marked `failed`
- [ ] **Isolation:** tenant B's key gets `404` for tenant A's `job_id`; `tenant_id` in the body is ignored/rejected
- [ ] `JobRepo`: valid transitions succeed, invalid ones (e.g. `succeeded→running`) raise; RLS blocks cross-tenant reads/writes (as `app_user`)
- [ ] `run_job` (called directly, real DB): success path stores result; handler exception ⇒ `failed` + error text; wrong-tenant `job_id` is not found and not touched
- [ ] `make_thread_id`/`parse_thread_id` round-trip; reject empty parts and `:`
- [ ] `/healthz` is `200` with no deps; `/readyz` is `503` when a dependency is unavailable
- [ ] No sync blocking calls in async handlers (checked by review + test using an event-loop-blocking detector or `asyncio` debug slow-callback warning)

### Gate 2 — Agent Behavior
- [ ] **N/A** — only a stub `echo` handler; no LangGraph agents yet

### Gate 3 — Live Smoke Test
- [ ] Start Redis (`docker run -d -p 6379:6379 redis:7`), run `python -m agents.migrate`, mint a key with `python -m agents.admin create-api-key tenant-a`
- [ ] `uvicorn api.main:app` and `arq workers.settings.WorkerSettings` running together
- [ ] `curl -s -X POST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d '{"kind":"echo","payload":{"hi":1}}'` ⇒ `202` in well under a second with a `job_id`
- [ ] `curl -s localhost:8000/v1/jobs/$JOB_ID -H "Authorization: Bearer $KEY"` ⇒ eventually `succeeded` with echoed result
- [ ] With the worker stopped, a `POST` still returns `202` and the job stays `queued` (proves the API doesn't block on work); starting the worker drains it
- [ ] `psql` as admin: `SELECT tenant_id, status FROM jobs;` shows the job under `tenant-a`

### Definition of Done
- [ ] All gates pass; Verification Report (Pass/Fail) output
- [ ] Docstring rule satisfied in every new file
- [ ] `docs/build-logs/YYYY-MM-DD-day-XX.md` written; README progress table updated (Spec 003 → Done)

## Open Questions for Review
1. **Auth:** hashed per-tenant API keys (recommended — small, real isolation now) vs. a trusted `X-Tenant-ID` header as a placeholder until a real auth spec (not recommended: spoofable).
2. **Async persistence:** add an async session + `JobRepo` and keep sync repos (recommended) vs. run the sync repos in a threadpool.
3. **Job records:** persist every job in Postgres (recommended — status API, audit, HITL later) vs. rely on ARQ's Redis job results only.
