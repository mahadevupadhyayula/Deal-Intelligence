# Spec 004 — ICP Segments, Versioning & Deterministic Scoring

**Status:** Done
**Phase:** 2 · **Depends on:** 001, 002, 003

## Objective
Let a tenant define its Ideal Customer Profile as one or more **independent segments** (e.g. a *primary* and a *secondary* ICP), each with **versions** over time, made of weighted criteria. Provide a **deterministic scoring engine** that turns per-criterion verdicts into a score and fit tier per segment, and picks the best-matching segment for a lead. No LLM runs here: Spec 005's agent gathers evidence and judges criteria, but numbers and tiers always come from this spec's code, so they are explainable, testable and consistent.

## Decisions Taken (from brainstorm and review)
- **Tenancy:** `tenant_id` stays on ICP tables (it identifies whose ICP it is and keeps the "tenant_id everywhere" rule). The platform is used by your own company for now, so there is one seeded tenant and **no tenant-management features**. All of the tenant's leads are matched against the same active segments; ICPs are never per-lead. `tenant_id` always means "the account that owns this row" (see CLAUDE.md glossary): a lead is data owned by a tenant, never a tenant itself. If a lead later converts into a customer it gets its own new tenant, linked from the lead by a separate field in a later spec (not part of this one).
- **Segments:** independent segments scored separately; the best match wins.
- **Qualitative attributes:** described (rubric) criteria judged by the LLM against evidence; missing evidence stays `unknown`, never a guess.
- **Verdict output:** score 0–100 + tier + per-criterion evidence.

## Technical Requirements

### 1. Models — `agents/schemas/icp.py`
- **`IcpSegment` (versioned definition, `TenantModel`):** `segment_key` (slug, e.g. `primary-hubspot-consultancy`), `version`, `name`, `rank: "primary" | "secondary"`, `description`, `criteria` (1–25), `personas`, `thresholds {strong=75, moderate=50, weak=25}` (strictly descending), `min_evidence_ratio` (0–1, default 0.5), `created_at`. `IcpSegmentIn` = same minus `tenant_id`, `segment_key`, `version`, `created_at` (server-assigned).
- **Criteria** (discriminated on `kind`), all with `id` (slug, unique within the segment), `label`, `role: "scoring" | "disqualifier"`, and for scoring criteria an integer `weight` 1–10:
  - `numeric`: `metric` (e.g. `employee_count`) and `bands: [{grade: ideal|acceptable|secondary, min?, max?}]` (each with at least one bound, inclusive; **bands may overlap and the best matching grade wins**). Example: 8–20 `ideal`, 5–30 `acceptable`, 31–50 `secondary`. The grade is **computed in code** from the observed value, not chosen by the LLM.
  - `judged`: `rubric` (what to look for), optional `grade_hints` `{ideal, acceptable, secondary}` (text describing each grade) and `non_match_examples`. The LLM selects a grade from evidence. Covers geography tiers, company type, services, CRM ecosystem, delivery complexity, commercial readiness, business model, operating maturity.
- **Grades and points:** `ideal = 1.0`, `acceptable = 0.75`, `secondary = 0.5`, `none = 0`, `unknown` (excluded from the denominator). For a disqualifier, any grade other than `none`/`unknown` means "present" (triggered).
- **`personas`:** list of `{tier: 1|2, label, roles: list[str]}` (e.g. Tier 1 economic buyers, Tier 2 pain-discovery roles). Validated and stored, **not scored**; a later spec uses them for contact research.
- **`CriterionVerdict(TenantModel)`:** `criterion_id`, `grade`, `observed_value: str | float | None`, `evidence_urls: list[AnyUrl]`, `rationale`.
- **`SegmentScore`:** `segment_key`, `version`, `rank`, `score` (0–100), `tier` (`strong|moderate|weak|not_fit|insufficient_data`), `evidence_ratio`, `contributions: dict[criterion_id, float]`, `disqualified_by`, `unverified_disqualifiers`.
- **`IcpResult`:** `segments: list[SegmentScore]`, `best_segment_key`, `best_rank`, `tier` (overall), `score` (best segment's).

### 2. Scoring engine — `agents/icp/scoring.py` (pure functions, no I/O)
`score_segment(segment, verdicts) -> SegmentScore`:
- Missing verdicts ⇒ `unknown`; verdicts for non-existent criterion ids ⇒ `ValueError`.
- Numeric criteria: grade derived from `observed_value` and the bands (outside all bands ⇒ `none`; no value ⇒ `unknown`); any LLM-supplied grade for a numeric criterion is ignored.
- `known_weight` = sum of weights of non-`unknown` scoring criteria; `evidence_ratio = known_weight / total_scoring_weight`; `score = 100 · Σ(weight · points) / known_weight` (0 when `known_weight = 0`).
- Disqualifier triggered ⇒ tier `not_fit`, score 0, listed in `disqualified_by`. An `unknown` disqualifier does not change the result but is listed in `unverified_disqualifiers`.
- Tier: `evidence_ratio < min_evidence_ratio` ⇒ `insufficient_data` (score still reported); otherwise thresholds as inclusive lower bounds (`≥strong`, `≥moderate`, `≥weak`, else `not_fit`).

`evaluate_segments(segments, verdicts_by_segment) -> IcpResult`:
- Overall tier = best **scored** tier among segments when it is `strong` or `moderate`; ties broken by higher score, then `primary` over `secondary`.
- Otherwise, if any segment is `insufficient_data`, the overall tier is `insufficient_data` (a better match may be hiding in the unknowns); otherwise the best of `weak`/`not_fit`.
- The result always names the best segment and its rank, so "strong secondary" is distinguishable from "strong primary".

### 3. Storage — migration `0004_icp_segments.sql`
- `icp_segments(tenant_id, segment_key, version int, name, rank, definition jsonb, is_active bool, created_at, PK (tenant_id, segment_key, version))`, FK to `tenants`, **forced RLS**, `app_user` gets `SELECT, INSERT, UPDATE`.
- Partial unique index `(tenant_id, segment_key) WHERE is_active` ⇒ at most one active version per segment, enforced by the database.
- Versions are **immutable**: changing a segment inserts a new version and deactivates the previous one in one transaction. Retiring a segment deactivates its active version (history kept). Evaluations (Spec 005) record the `{segment_key: version}` map they used.

### 4. Repository — `agents/repos/icp.py` (async)
- `IcpRepo(AsyncTenantSession)`: `create_version(segment_key, IcpSegmentIn)` (takes `pg_advisory_xact_lock` on `(tenant_id, segment_key)` so concurrent writers get distinct, gapless versions), `get_active(segment_key)`, `list_active()`, `get_version(segment_key, n)`, `list_versions(segment_key)`, `retire(segment_key)`. Explicit `WHERE tenant_id` plus RLS.

### 5. API — `api/routes/icp.py`
All routes require the Spec 003 Bearer key; tenant comes only from the key.
- `PUT /v1/icp/segments/{segment_key}` body `IcpSegmentIn` → `201 {segment_key, version}`; becomes the active version. Invalid ⇒ `422` with field-level errors; body `tenant_id`/`version`/`segment_key` ⇒ `422`.
- `GET /v1/icp/segments` → all active segments; `GET /v1/icp/segments/{key}` → active version (`404` if none/retired).
- `GET /v1/icp/segments/{key}/versions` and `.../versions/{n}` → history (`404` if missing or another tenant's).
- `DELETE /v1/icp/segments/{key}` → `204`, retires the segment.
- Any valid tenant key may edit ICPs for now (roles are out of scope). Provisioning the single tenant uses the existing `python -m agents.admin create-tenant` / `create-api-key`.

### 6. Examples + docs
- `docs/examples/icp/primary_hubspot_consultancy.json`: your primary ICP encoded as a segment (company type, size bands 8–20 / 5–30, tiered geography, target customers, core services, HubSpot ecosystem, delivery complexity, commercial readiness, business model, operating maturity; personas Tier 1 and Tier 2).
- `docs/examples/icp/secondary_hubspot_consultancy.json`: a secondary segment (31–50 employees as your starting point; see open question 1).
- Both are used by tests and the smoke test. WHAT/WHERE/WHY docstring in every new file; comments explaining scoring trade-offs (unknowns excluded, evidence floor, numeric grades computed in code).

## Out of Scope
- Researching companies or LLM judging of criteria (Spec 005)
- Contact/persona research and outreach targeting (later spec; personas are stored only)
- ICP editor UI (Spec 014); roles/permissions; audit of who changed an ICP; tenant-management features
- ICPs learned from won/lost deals or CRM data; re-scoring existing leads when an ICP changes; routing/notifications by tier

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [x] Model validation: duplicate criterion ids; no scoring criterion; weight out of 1–10; weight on a disqualifier; boundless numeric bands or `min > max`; non-descending thresholds; bad `segment_key`; unknown fields — all rejected
- [x] Scoring table tests (parametrised, exact numbers): all `ideal` ⇒ 100; all `none` ⇒ 0; `acceptable`/`secondary` give 0.75/0.5 credit; weights change results as expected; thresholds inclusive at boundaries
- [x] Numeric criteria: 12 ⇒ ideal, 25 ⇒ acceptable, 40 ⇒ secondary, 60 ⇒ none, no value ⇒ unknown; an LLM-supplied grade on a numeric criterion is ignored
- [x] Unknown handling: excluded from the denominator; `evidence_ratio` below the floor ⇒ `insufficient_data` with the score still reported; all unknown ⇒ `insufficient_data`, score 0
- [x] Disqualifier triggered ⇒ `not_fit`, score 0, listed; unknown disqualifier ⇒ listed as unverified, no tier effect
- [x] Missing verdicts treated as unknown; verdict for a non-existent criterion id ⇒ `ValueError`
- [x] `evaluate_segments`: best segment chosen by tier then score then primary-over-secondary; strong secondary reported as secondary; one segment `insufficient_data` and others weak ⇒ overall `insufficient_data`; all `not_fit` ⇒ `not_fit`
- [x] Scoring is deterministic and does no I/O (runs with no DB/network)
- [x] `IcpRepo` (as `app_user`): versions increment per segment; exactly one active version per segment; old versions readable; retire leaves history; two concurrent `create_version` calls yield distinct versions
- [x] **Isolation:** tenant B cannot read, list, retire or activate tenant A's segments/versions; both tenants may have `primary…` version 1; RLS fail-closed with no tenant set
- [x] API: `401` without a valid key; `PUT` valid ⇒ `201`; invalid ⇒ `422`; body `tenant_id` ⇒ `422`; list/get/versions correct; `DELETE` ⇒ `204` then `GET` ⇒ `404`; cross-tenant lookups ⇒ `404`
- [x] Both example files validate as `IcpSegmentIn`

### Gate 2 — Agent Behavior
- [x] **N/A** — no LangGraph agent or LLM in this spec; scoring is pure and fully covered by Gate 1 tests

### Gate 3 — Live Smoke Test
- [x] `python -m agents.migrate` applies `0004`; `psql` shows `icp_segments` with RLS enabled+forced and the partial unique index
- [x] Seed the single tenant and key with the admin CLI; with the API running, `PUT` both example files to `/v1/icp/segments/primary-hubspot-consultancy` and `.../secondary-hubspot-consultancy` ⇒ `201` with version 1; `GET /v1/icp/segments` returns both
- [x] A second `PUT` of the primary ⇒ version 2 active; `/versions` lists both; `psql` shows exactly one `is_active` row per segment
- [x] `DELETE` of the secondary ⇒ `204`; it disappears from `GET /v1/icp/segments` but its history remains in `psql`
- [x] A second tenant's key sees an empty list and `404` on tenant A's segment and versions

### Definition of Done
- [x] All gates pass; Verification Report (Pass/Fail) output
- [x] Docstring rule satisfied in every new file
- [x] `docs/build-logs/YYYY-MM-DD-day-XX.md` written; README progress table updated (Spec 004 → Done)

## Open Questions for Review
1. **Secondary segment:** your example gives 31–50 employees as the secondary band. Is the secondary ICP "the same profile, larger firms" (I would encode exactly that, with its own thresholds), or a distinct profile with different criteria? If distinct, please share its definition.
2. **Grade points:** `ideal 1.0 / acceptable 0.75 / secondary 0.5`, thresholds 75/50/25 and evidence floor 0.5 — sensible defaults for your team?
3. **Disqualifiers:** a single triggered disqualifier forces `not_fit` (recommended); none are in your example, so do you have any (e.g. enterprise SI, non-HubSpot shop)?
4. **Commercial readiness** (3+ concurrent implementations or 2+ onboardings/month) is rarely public; I would encode it as a low-weight `judged` criterion that will usually be `unknown` and so not hurt the score. OK?

## Resolution of Open Questions (implemented defaults)
1. Secondary segment shipped as a **placeholder** (same profile, larger firms: 31–50 ideal, 51–75 acceptable) pending the owner's real definition.
2. Grade points 1.0 / 0.75 / 0.5, thresholds 75/50/25 and evidence floor 0.5 kept as defaults (all configurable per segment except grade points, which are constants in `agents/schemas/icp.py`).
3. No disqualifiers in the shipped examples; the mechanism is implemented and tested.
4. Commercial readiness shipped as a weight-2 judged criterion.
