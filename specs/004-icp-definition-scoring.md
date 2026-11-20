# Spec 004 — ICP Definition & Deterministic Scoring

**Status:** Draft
**Phase:** 2 · **Depends on:** 001, 002, 003

## Objective
Give every tenant a place to define their Ideal Customer Profile (ICP) as **structured, weighted criteria**, and provide a **deterministic scoring engine** that turns per-criterion verdicts into a score and fit tier. No LLM is involved here. Spec 005's Lead Research agent will gather evidence and have an LLM judge each criterion, but the number and tier always come from the code in this spec, so they are explainable, testable and consistent across runs.

## Decisions Taken (from brainstorm)
- ICP format: structured weighted criteria (not free text).
- ICP definition is its own spec (this one); Spec 005 only *evaluates* against it.
- Verdict output: score 0–100 + tier (`strong | moderate | weak | not_fit | insufficient_data`) with per-criterion evidence.

## Technical Requirements

### 1. Models — `agents/schemas/icp.py`
- **Criteria** (discriminated union on `type`), each with `id` (slug, unique within the ICP), `label`, `role: "scoring" | "disqualifier"`, and for scoring criteria an integer `weight` 1–10 (disqualifiers have no weight):
  - `industry` `{values: list[str]}`
  - `employee_range` `{min?: int, max?: int}` (at least one bound; `min ≤ max`)
  - `geography` `{regions: list[str]}`
  - `tech_stack` `{any_of: list[str]}`
  - `signal` `{description: str}` — free-form buying signal (e.g. "raised Series B in the last 18 months")
- `IcpDefinition(TenantModel)`: `version: int`, `name`, `criteria` (1–20), `thresholds {strong=75, moderate=50, weak=25}` (strictly descending), `min_evidence_ratio` (0–1, default 0.5), `created_at`. Validation: ≥ 1 scoring criterion, unique ids, `extra="forbid"`.
- `IcpDefinitionIn` (API input): same fields minus `tenant_id`, `version`, `created_at` (the server assigns them).
- `CriterionVerdict(TenantModel)`: `criterion_id`, `verdict: "match" | "partial" | "no_match" | "unknown"`, `evidence_urls: list[AnyUrl]`, `rationale: str`.
- `IcpScore(TenantModel)`: `score: float` (0–100), `tier`, `evidence_ratio`, `contributions: dict[criterion_id, float]`, `disqualified_by: list[str]`, `unverified_disqualifiers: list[str]`.

### 2. Scoring engine — `agents/icp/scoring.py` (pure function, no I/O)
`score_icp(icp, verdicts) -> IcpScore`:
- Missing verdicts are treated as `unknown`; verdicts for unknown criterion ids are rejected (`ValueError`).
- Points: `match = 1.0`, `partial = 0.5`, `no_match = 0.0`, `unknown` excluded from the score's denominator.
- `known_weight` = sum of weights of non-`unknown` scoring criteria; `evidence_ratio = known_weight / total_scoring_weight`.
- `score = 100 · Σ(weight · points) / known_weight` (0 when `known_weight = 0`).
- **Disqualifier** with verdict `match` ⇒ tier `not_fit`, score 0, listed in `disqualified_by`. A disqualifier that is `unknown` does not change the result but is listed in `unverified_disqualifiers` so humans see what was not checked.
- Tier: if `evidence_ratio < min_evidence_ratio` ⇒ `insufficient_data` (score still reported); else by thresholds (`≥ strong` → strong, `≥ moderate` → moderate, `≥ weak` → weak, else `not_fit`). Thresholds are inclusive lower bounds.
- Thin evidence never silently becomes a good or bad verdict; it becomes `insufficient_data`.

### 3. Storage — migration `0004_icp_definitions.sql`
- `icp_definitions(tenant_id, version int, name, definition jsonb, is_active bool, created_at, PK (tenant_id, version))`, FK to `tenants`, **forced RLS** (same policy as Spec 002/003), `app_user` gets `SELECT, INSERT, UPDATE`.
- Partial unique index `(tenant_id) WHERE is_active` ⇒ at most one active ICP per tenant, enforced by the database.
- Versions are **immutable**: a change creates a new version and deactivates the previous one in one transaction. Later evaluations record the version they used.

### 4. Repository — `agents/repos/icp.py` (async)
- `IcpRepo(AsyncTenantSession)`: `create_version(IcpDefinitionIn) -> IcpDefinition` (takes `pg_advisory_xact_lock(hashtext(tenant_id))` so concurrent writers get distinct, gapless versions), `get_active()`, `get_version(n)`, `list_versions()`. Explicit `WHERE tenant_id` plus RLS, as elsewhere.

### 5. API — `api/routes/icp.py`
All routes require the Spec 003 Bearer key; tenant comes only from the key.
- `PUT /v1/icp` body `IcpDefinitionIn` → `201 {version}`; becomes the active ICP. Invalid ICP ⇒ `422` with field-level errors; body `tenant_id`/`version` ⇒ `422`.
- `GET /v1/icp` → active ICP (`404` if the tenant has none).
- `GET /v1/icp/versions` → list of `{version, name, is_active, created_at}`; `GET /v1/icp/versions/{n}` → that version (`404` if missing or another tenant's).
- Any valid tenant key may edit the ICP for now (roles/permissions are out of scope).

### 6. Sample + docs
- `docs/examples/icp_b2b_saas.json`: a realistic sample ICP used by tests and the smoke test.
- WHAT/WHERE/WHY docstring in every new file; comments explaining the scoring trade-offs (unknowns excluded, evidence floor).

## Out of Scope
- Researching companies or LLM judging of criteria (Spec 005)
- ICP editor UI (Spec 014); roles/permissions; audit log of who changed the ICP
- Multiple simultaneous ICPs/segments per tenant; ICP learned from won/lost deals or CRM data
- Re-scoring existing leads when the ICP changes; routing/notifications by tier

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] Model validation: duplicate ids, no scoring criterion, weight out of 1–10, disqualifier with weight, `employee_range` with no bound or `min > max`, non-descending thresholds, unknown fields — all rejected
- [ ] Scoring table tests (parametrised, exact numbers): all match ⇒ 100/strong; all no_match ⇒ 0; partial = half credit; weights change the result as expected; thresholds are inclusive at boundaries
- [ ] Unknown handling: unknowns excluded from the denominator; `evidence_ratio` below the floor ⇒ `insufficient_data` with the score still reported; all unknown ⇒ `insufficient_data`, score 0
- [ ] Disqualifier `match` ⇒ `not_fit`, score 0, listed; unknown disqualifier ⇒ listed as unverified, no effect on tier
- [ ] Missing verdicts treated as unknown; verdict for a non-existent criterion id ⇒ `ValueError`
- [ ] Scoring is deterministic and does no I/O (same input ⇒ same output; test runs with no DB/network)
- [ ] `IcpRepo` (as `app_user`): version numbers increment per tenant; exactly one active version; old versions stay readable; two concurrent `create_version` calls yield distinct versions
- [ ] **Isolation:** tenant B cannot read, list or activate tenant A's ICP versions; both tenants may have version 1; RLS fail-closed with no tenant set
- [ ] API: `401` without a valid key; `PUT` valid ⇒ `201`; invalid ⇒ `422`; body `tenant_id` ⇒ `422`; `GET` with no ICP ⇒ `404`; version history correct; cross-tenant version lookup ⇒ `404`
- [ ] Sample `docs/examples/icp_b2b_saas.json` validates as `IcpDefinitionIn`

### Gate 2 — Agent Behavior
- [ ] **N/A** — no LangGraph agent or LLM in this spec; the scoring logic is pure and fully covered by Gate 1 tests

### Gate 3 — Live Smoke Test
- [ ] `python -m agents.migrate` applies `0004`; `psql` shows `icp_definitions` with RLS enabled+forced and the partial unique index
- [ ] With API running and a minted key: `curl -X PUT localhost:8000/v1/icp -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d @docs/examples/icp_b2b_saas.json` ⇒ `201 {"version":1}`; `GET /v1/icp` returns it
- [ ] A second `PUT` ⇒ version 2 active; `GET /v1/icp/versions` lists both; `psql` shows exactly one `is_active` row for the tenant
- [ ] A second tenant's key sees `404` on `GET /v1/icp` and cannot fetch tenant A's `/versions/1`

### Definition of Done
- [ ] All gates pass; Verification Report (Pass/Fail) output
- [ ] Docstring rule satisfied in every new file
- [ ] `docs/build-logs/YYYY-MM-DD-day-XX.md` written; README progress table updated (Spec 004 → Done)

## Open Questions for Review
1. **Scoring numbers:** `match=1.0 / partial=0.5`, default thresholds 75/50/25 and evidence floor 0.5 — are these sensible defaults for how your sales team thinks about fit?
2. **Disqualifier semantics:** a single disqualifier match forces `not_fit` regardless of score (recommended). Alternative: treat as a heavy penalty instead.
3. **Weights:** integers 1–10 (recommended, easy to explain) vs. free percentages that must sum to 100.
4. **Permissions:** any valid tenant API key may change the ICP for now; acceptable until a roles spec exists?
