# Spec 005 — Lead Research Agent with ICP Evaluation (LangGraph)

**Status:** Done; Gates 1, 2 and 3 pass (Gate 3 calibration set not run: no labels supplied)
**Phase:** 2 · **Depends on:** 001, 002, 003, 004

## Objective
Ship the first real agent: given a company, research it on the web, **judge it against the tenant's active ICP segments (Spec 004)**, and persist both a source-cited `LeadProfile` and an `IcpEvaluation` (best-matching segment and rank, score, tier, per-segment criterion verdicts with verified evidence quotes). It runs as a `lead_research` job through the Spec 003 queue (never inline in FastAPI) as an explicit, bounded LangGraph pipeline. The LLM only gathers facts and judges rubrics; **every claim must be backed by a quote that code verifies appears in the cited source**, numeric criteria are graded in code, and the **score and tier come from Spec 004's engine**, never from the LLM. External dependencies sit behind small interfaces so tests are deterministic and free; real services are used only in Gate 3.

## Decisions Taken
From brainstorm:
- **LLM:** provider-agnostic via LangChain `init_chat_model("<provider>:<model>")`, configured by `LLM_MODEL`.
- **Search:** Tavily behind a `SearchTool` interface, plus a fixture adapter.
- **Graph:** explicit pipeline with at most **one** refine loop (not free-roaming ReAct).
- **ICP:** independent versioned segments (Spec 004); a lead is evaluated against all active segments and the best match is reported.
- **Verdict:** best segment + score 0–100 + tier + per-criterion evidence.

From review (this revision):
- **Evidence = URL + verbatim quote**, verified in code (not just "URL was fetched").
- **The LLM never sees or emits `tenant_id` or segment keys**; its output is keyed by neutral criterion keys and code builds the tenant-stamped verdicts.
- **Identical criteria are judged once** (deduplicated across segments) and numeric metrics are extracted once.
- **Entity check:** a verdict is only trusted if sources are confirmed to be about the right company.
- **Refine only when it could change the tier** (computed with best/worst-case bounds).
- **List endpoint included** (rank leads by fit); **labelled calibration set** runs when the owner supplies labels and is reported, not gating.
- **No active ICP ⇒ job fails fast; evaluation history is kept; handlers get a `JobContext`; LangSmith tenant ids are hashed.**

## Technical Requirements

### 1. Dependencies and config
- `pyproject.toml`: `langgraph`, `langchain`, `langchain-core`, `langsmith`, `tavily-python`; provider packages as optional extras (`llm-anthropic`, `llm-openai`). These are **worker-only** imports (see §8).
- `agents/config.py` + `.env.example` (no secrets in code): `llm_model`, `tavily_api_key` (`SecretStr`), `research_max_searches` (8), `research_max_llm_calls` (8, retries included), `research_max_refine_loops` (1), `research_max_sources` (8), `research_source_char_limit` (6000 per source), `research_max_total_chars` (40000 sent to the LLM), `research_retry_attempts` (2), `research_retry_backoff_seconds` (1.0), `research_run_timeout_seconds` (270), `job_timeout_seconds` (300, ARQ's outer limit).

### 2. Interfaces — `agents/lead_research/tools.py`, `llm.py`
- `SearchResult(url, title, content)`; `SearchTool` protocol `async search(query, max_results)`; `TavilySearch` (async client, per-call timeout) and `FixtureSearch`. Errors map to `SearchError` (transient: timeout/429/5xx; permanent: other).
- `StructuredLLM` protocol: `async generate(schema: type[T], system: str, user: str) -> T`; production wraps `init_chat_model(...).with_structured_output(schema)`; tests use a scripted double. The graph depends only on this protocol.
- **Retries:** both adapters retry **transient** errors up to `research_retry_attempts` with exponential backoff; permanent errors fail immediately. Each LLM retry counts toward `llm_calls_used`, each search retry toward `searches_used`, so the budget stays honest.

### 3. Models (`agents/lead_research/models.py`, `agents/schemas/evaluation.py`)
LLM-facing (no `tenant_id`, no segment keys, `extra="forbid"`):
- `Evidence{url: str, quote: str (10–400 chars)}`.
- `MetricDraft{value: float | None, evidence: list[Evidence]}`.
- `JudgementDraft{grade: ideal|acceptable|secondary|none|unknown, evidence: list[Evidence], rationale: str}`.
- `ResearchFindings{official_domain: str | None, industry: str | None, signals: list[str], metrics: dict[metric_name, MetricDraft], judgements: dict[criterion_key, JudgementDraft]}`. `metrics` are keyed by the `metric` names of numeric criteria across all segments; `judgements` by neutral keys (`c1`, `c2`, …) assigned in code, one per **distinct** judged criterion (same rubric + hints + non-match examples ⇒ one key shared by every segment that uses it).

Persisted/returned (tenant-stamped by code):
- `StoredVerdict(CriterionVerdict)` adds `evidence_quotes: list[str]`; built only by the validate step.
- `IcpEvaluation(TenantModel)`: `evaluation_id`, `lead_id`, `job_id`, `icp_versions: dict[segment_key, int]`, `best_segment_key`, `best_rank`, `score`, `tier`, `segments: list[SegmentScore]`, `verdicts: dict[segment_key, list[StoredVerdict]]`, `entity_confirmed: bool`, `resolved_domain: str | None`, `flags: list[str]`, `status: complete | partial`, `created_at`.
- `LeadProfile` (Spec 001) is reused unchanged: `industry`, `employee_count` (from the verified `employee_count` metric), `domain`, `signals`, `sources`, `confidence`. Decision: richer attributes (geography, services, CRM ecosystem...) live in the evaluation's judgements and quotes, not as new profile columns; `LeadProfile.confidence` is documented as **profile completeness/sourcing**, distinct from ICP fit.

### 4. Graph — `agents/lead_research/graph.py`, `state.py`, `nodes.py`
State (`TypedDict`): `tenant_id, lead_id, job_id, company_name, domain, segments, criterion_keys, queries, sources, findings, verdicts, entity_confirmed, resolved_domain, flags, result, refine_count, searches_used, llm_calls_used, status, profile, evaluation`.

1. `load_icp` (no LLM): load active segments; build `criterion_keys` (dedupe identical judged criteria, collect metric names). **No active segment ⇒ the job fails fast** with `"no active ICP defined"` (no research, no spend).
2. `resolve_entity` (no LLM): if the payload has `domain`, use it; otherwise one search for the official site and take the host of the top result whose title/content contains the company name (case-insensitive). Result: `resolved_domain` or `None`.
3. `plan_queries` (LLM): 2–4 queries covering core firmographics and the distinct criteria; on a refine pass, only criteria still `unknown`.
4. `search` (no LLM): run queries, dedupe by normalized URL, rank sources on the resolved domain first, keep at most `research_max_sources`, truncate each to `research_source_char_limit` and the whole set to `research_max_total_chars`. The **truncated text is what the LLM sees and what quotes are verified against**.
5. `synthesize` (LLM, one structured call): returns `ResearchFindings`. Prompt rules: *no evidence ⇒ `unknown`, never guess; every claim needs a verbatim quote from one source; use the given rubrics and grade hints*.
6. `validate` (no LLM), in code:
   - (a) normalise URLs (lowercase host, strip fragment/trailing slash) and map each evidence URL to a fetched source, else drop it;
   - (b) **verify each quote**: after case/whitespace/quote-mark normalisation it must be a substring of the cited source text; failing evidence is dropped;
   - (c) a judgement whose grade is not `unknown` but has **no verified evidence** becomes `unknown` (this includes `none`: absence of evidence is not evidence of absence); a metric with a value but no verified evidence becomes `None`;
   - (d) discard judgement keys/metric names not requested; any requested key the LLM skipped becomes `unknown`;
   - (e) **expand into per-segment `StoredVerdict`s** with `tenant_id` from state: judged criteria take the shared judgement, numeric criteria take `observed_value` from the metric (so one extracted number feeds every segment's bands and the profile);
   - (f) `entity_confirmed` = at least one verified-evidence source is on `resolved_domain` and contains the company name; if the payload gave no domain and none was resolved, it is `False`;
   - (g) flag `single_source_disqualifier` when a triggered disqualifier rests on only one verified evidence item;
   - (h) compute profile `confidence` (below) and list `unknown_criteria`.
7. `score` (no LLM): `evaluate_segments` (Spec 004) ⇒ per-segment scores, best segment/rank, overall tier. If `entity_confirmed` is `False`, the overall tier is overridden to `insufficient_data` and the flag `entity_unconfirmed` is added (the numeric score is kept for information).
8. Route — **refine only if it could change the outcome**: compute `pessimistic` and `optimistic` overall tiers via `evaluate_segments(..., assume_unknown="none"|"ideal")` (see §9). If they differ, `refine_count < max`, and budget remains ⇒ back to `plan_queries`; otherwise ⇒ `persist`. A lead whose only unknown criterion is a weight-2 "commercial readiness" therefore does **not** trigger a second pass.
9. `persist`: in **one transaction**, upsert `LeadProfile` (`AsyncLeadRepo`) and insert the `IcpEvaluation` (`AsyncEvaluationRepo`). Idempotent: unique `(tenant_id, job_id)`, `ON CONFLICT DO NOTHING`. Persisted even when sources are empty (`no_results` ⇒ empty profile, `insufficient_data`).

- **Budget guard:** when searches, LLM calls or sources hit their caps, finish with `status="partial"` using what exists; never loop past a cap.
- Compiled with an **optional checkpointer** (none here; Spec 008 supplies `PostgresSaver`); invoked with `thread_id = make_thread_id(tenant_id, lead_id, job_id)`.
- Profile `confidence` = `0.4·min(n_verified_sources,3)/3 + 0.4·(filled_fields/4) + 0.2·(domain confirmed)`, clamped to [0,1]; computed, not LLM-reported.
- Design notes: one combined `synthesize` call halves cost and grounds verdicts in the same evidence; judging distinct criteria once avoids inconsistent grades for the same attribute across segments; the pessimistic/optimistic bound makes "should we research more?" a cheap deterministic decision instead of a heuristic.

### 5. Prompt-injection baseline (full guardrails are Spec 012)
- Fetched text is wrapped as `<untrusted_source id=… url=…>…</untrusted_source>`; the system prompt says contents are data, never instructions (including claims about ICP fit).
- The synthesis call has **no tools**; output must validate against `ResearchFindings`. The LLM cannot set score, tier, tenant or segment; those are computed or stamped in code.
- Quote verification means injected text cannot create evidence out of thin air, and cannot fabricate a quote that is not on a fetched page.
- Honest limit: these are structural defences. A fetched page can still contain a persuasive false statement that gets quoted and cited; flagged single-source disqualifiers (step 6g) and the later HITL review (Spec 008) are the mitigation. Adversarial model evals are Spec 013.

### 6. Persistence — migration `0005_lead_icp_evaluations.sql`, async repos
- `lead_icp_evaluations(tenant_id, evaluation_id uuid, lead_id, best_segment_key, best_rank, score, tier, status, job_id, data jsonb, created_at, PK (tenant_id, evaluation_id), UNIQUE (tenant_id, job_id))`, index `(tenant_id, lead_id, created_at DESC)`, index `(tenant_id, tier, score DESC)`, **forced RLS**, `app_user` `SELECT, INSERT`. **History is kept**: each run inserts a row; "latest" is by `created_at`. `LeadProfile` is overwritten on re-research (the evaluation history keeps the evidence).
- `AsyncLeadRepo` (`upsert`, `get`) and `AsyncEvaluationRepo` (`create`, `get_latest(lead_id)`, `list(lead_id)`, `list_latest(tier=None, limit, cursor)`) on `AsyncTenantSession`; same RLS + explicit `WHERE tenant_id` contract.

### 7. Job integration (amends Spec 003)
- Handlers receive a `JobContext` (`pool`, `search`, `llm`) as first argument: `handler(ctx, tenant_id, job_id, payload)`; `echo` and its tests are updated; `workers/settings.py` builds the context at startup (real adapters) and sets `function(run_job, timeout=job_timeout_seconds)`.
- **Timeouts:** `run_job` wraps the handler in `asyncio.timeout(research_run_timeout_seconds)` (below ARQ's limit) so a timed-out run is recorded as `failed: timed out after Ns` instead of staying `running`. *Known limitation (unchanged from Spec 003):* a hard worker crash can still strand a `running` job; stale-job recovery is out of scope.
- Payload `LeadResearchInput(lead_id, company_name, domain?)`, `extra="forbid"`; invalid ⇒ job `failed` with a message. Job `result` = `{status, profile, evaluation}` JSON.

### 8. API-process isolation and kinds (amends Spec 003)
- New `agents/job_kinds.py` holds only the **names** of valid job kinds (`echo`, `lead_research`); the API validates against it. The handler registry moves to `workers/handlers.py`, which alone imports `agents.lead_research` (LangGraph/LangChain). Importing `api.main` must not import `langgraph` or `langchain` (tested).

### 9. Amendments to Spec 004's scoring module
- `score_segment` and `evaluate_segments` gain `assume_unknown: Literal["none","ideal"] | None = None` (default keeps current behaviour). When set, every `unknown` **scoring** criterion (numeric included) is treated as that grade, which makes `evidence_ratio = 1`; unknown disqualifiers are treated as `none` (not triggered). Used only to compute the pessimistic/optimistic tier bounds; never stored as a score.

### 10. Read API — `api/routes/leads.py`
- `GET /v1/leads/{lead_id}` → profile + latest evaluation; `GET /v1/leads/{lead_id}/evaluations` → history.
- `GET /v1/leads?tier=&best_rank=&limit=&cursor=` → latest evaluation per lead, ordered by score descending, keyset pagination on `(score, lead_id)` (default limit 50, max 200). This is how users rank leads by fit.
- Tenant from the key; another tenant's lead ⇒ `404`; invalid `tier`/`limit` ⇒ `422`.

### 11. Observability
- LangSmith tracing by env only; runs named `lead_research`, tagged with `{tenant_hash, job_id, lead_id, icp_versions}` where `tenant_hash` is a truncated SHA-256 of `tenant_id` (the raw id is not sent). API keys never enter state or metadata. Traces will still contain scraped company text.

### 12. Golden datasets — `tests/fixtures/lead_research/*.json`
Each case: input, segments (from `docs/examples/icp/primary_hubspot_consultancy.json` / `secondary_hubspot_consultancy.json` or variants), recorded search results, scripted LLM outputs, expected node sequence, expected queries, expected profile, and expected evaluation (tier, score, per-criterion verdicts). Cases:
`strong_primary_fit`, `strong_secondary_fit` (primary weak, secondary strong ⇒ best segment is the secondary), `clear_non_fit`, `disqualifier_hit` (incl. single-source flag), `insufficient_data`, `sparse_needs_refine` (unknowns that matter, resolved on pass two), `refine_skipped_when_tier_fixed` (only a low-weight unknown remains), `quote_not_in_source` (hallucinated quote ⇒ unknown), `verdict_without_evidence`, `numeric_without_evidence` (value cleared), `duplicate_criteria_judged_once`, `entity_unconfirmed`, `injection_attempt`, `llm_transient_error_retry`, `budget_exhausted` (partial), `run_timeout` (job failed, not stuck), `no_results`, `no_active_icp` (job fails).
**What Gate 2 proves, and what it does not:** with a scripted LLM, Gate 2 verifies orchestration, validation, evidence checking, scoring and budgets. It does **not** verify that a real model judges well; that is checked by the Gate 3 judgement checks and calibration set.

### 13. Docs
- WHAT/WHERE/WHY docstring in every new file; comments on the trade-offs (bounded loop, quote verification, computed scores, protocol seams, single synthesize call, dedupe).

## Out of Scope
- ICP definition/management, scoring rules and persona storage (Spec 004); ICP editor UI (Spec 014); contact/persona research using the ICP's personas
- Writing research into the LLM Wiki (Spec 006); CRM sync or writing the score back to the CRM (Spec 010+)
- `PostgresSaver` / HITL review of flagged results (Spec 008); full prompt-injection guardrails (Spec 012); LangSmith-hosted eval datasets and adversarial evals (Spec 013)
- Per-tenant API keys, budgets/quotas, rate limits, bulk lead submission (Specs 010/011 and later); per-tenant tracing opt-out
- Stale-job recovery after hard worker crashes; re-evaluating existing leads when an ICP changes; routing/notifications by tier; page fetching beyond Tavily text; result caching

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [x] Config: new settings load with defaults; `tavily_api_key` is a `SecretStr`, absent from `repr`
- [x] `TavilySearch` (HTTP mocked): maps results, applies timeout; transient errors retried with backoff then succeed; retries exhausted ⇒ `SearchError`; permanent errors not retried. Same retry behaviour for the LLM wrapper; retries count toward the budget
- [x] `ResearchFindings`/`LeadResearchInput`/`IcpEvaluation` reject unknown fields and bad values; **the LLM-facing JSON schema contains no `tenant_id` and no segment keys**
- [x] Validate: foreign/unnormalised URLs handled; a quote not present in the cited source is dropped; a non-`unknown` judgement (including `none`) with no verified evidence becomes `unknown`; a metric value with no verified evidence becomes `None`; skipped keys become `unknown`; unrequested keys discarded; per-segment `StoredVerdict`s carry the job's tenant
- [x] Criterion dedupe: identical judged criteria across segments share one key and one judgement; one `employee_count` metric feeds every segment's size bands and the profile
- [x] Entity check: confirmed/unconfirmed cases; unconfirmed ⇒ overall tier `insufficient_data` + `entity_unconfirmed` flag; `resolve_entity` picks the right host and rejects results that do not mention the company
- [x] Scoring amendment: `assume_unknown` none/ideal produce the expected bounds; default behaviour unchanged (all Spec 004 tests still pass)
- [x] Refine decision: bounds differ ⇒ refine; bounds equal (only low-weight unknowns) ⇒ no refine; caps respected
- [x] Prompt caps: at most `research_max_sources` sources and `research_max_total_chars` sent; untrusted wrapping on every source; budget guard ⇒ `status="partial"`
- [x] Profile confidence formula: boundary cases match expected numbers
- [x] `AsyncLeadRepo` / `AsyncEvaluationRepo`: round-trip; history retained; latest ordering; persisting the same `job_id` twice is a no-op; tenant B cannot read or write tenant A's rows (RLS, as `app_user`); `list_latest` filters by tier and paginates stably
- [x] Handler: valid job persists profile + evaluation under the **job's** tenant only, recording segment versions; no active ICP ⇒ `failed` with the message; invalid payload ⇒ `failed`; **inner timeout marks the job `failed: timed out`**; amended `echo` and all Spec 003 tests still pass
- [x] Read API: `401`; own lead `200`; cross-tenant `404`; list endpoint filters/sorts/paginates; bad params `422`
- [x] **API isolation:** importing `api.main` in a fresh interpreter does not import `langgraph` or `langchain`
- [x] No blocking calls in async paths (static guard extended to `agents/lead_research/` and `workers/`)

### Gate 2 — Agent Behavior (golden datasets)
- [x] For every fixture case: node sequence, search queries, final profile and evaluation (tier, score, per-criterion verdicts, flags) equal the golden expectations
- [x] `strong_primary_fit`, `strong_secondary_fit` (best segment = secondary), `clear_non_fit`, `disqualifier_hit` (tier `not_fit`, score 0, criterion listed, single-source flag), `insufficient_data` yield the expected results
- [x] `sparse_needs_refine` loops exactly once and second-pass queries target only unknown criteria; `refine_skipped_when_tier_fixed` does not loop; `budget_exhausted` ends `partial` without exceeding caps; `no_results` ends `insufficient_data` with an empty profile persisted
- [x] `quote_not_in_source`, `verdict_without_evidence`, `numeric_without_evidence` all end as `unknown` and cannot raise the score
- [x] `entity_unconfirmed` ⇒ `insufficient_data`; `llm_transient_error_retry` succeeds within the budget; `run_timeout` ⇒ job `failed`, not stuck `running`
- [x] `injection_attempt`: no injected fields/URLs in output, score computed by code, normal trajectory only (structural defence; see §5 limit)
- [x] `no_active_icp`: job fails before any search or LLM call
- [x] Two-tenant run: separate ICPs and results, no cross-tenant data in state or DB; `thread_id = tenant:lead:job`

### Gate 3 — Live Smoke Test (needs `TAVILY_API_KEY`, an LLM provider key, `LANGSMITH_API_KEY`)
- [x] Redis + Postgres up, migrations applied, tenant + API key minted, the two example segments loaded via `PUT /v1/icp/segments/{key}`, API and worker running with real adapters
- [x] `POST /v1/jobs {"kind":"lead_research","payload":{"lead_id":"l1","company_name":"<chosen consultancy>","domain":"<its domain>"}}` ⇒ `202`; job `succeeded` within the timeout
- [x] `GET /v1/leads/l1` shows a profile with ≥ 2 source URLs and an evaluation whose `icp_versions` match the active segments, `entity_confirmed: true`
- [x] **Audit checks on the stored result:** every non-`unknown` verdict has ≥ 1 evidence quote; re-fetching or re-running confirms each quote appears in its cited source text; re-running `evaluate_segments` on the stored verdicts reproduces the stored scores, best segment and tier exactly
- [x] Judgement check (LLM output varies, reported honestly): a real founder-led HubSpot implementation consultancy scores ≥ `moderate`; a clearly mismatched company (e.g. a local bakery) scores `weak`/`not_fit`
- [ ] **Calibration set** (when the owner supplies `tests/fixtures/lead_research/labelled_companies.json`, ~10 companies labelled fit / not-fit): run all live; report agreement (fit ⇒ strong/moderate, not-fit ⇒ weak/not_fit; `insufficient_data` counted separately) and list disagreements. *Reported, not gating, until labels exist; marked "not run" otherwise.*
- [x] `GET /v1/leads?tier=strong` lists the strong fits ordered by score
- [x] LangSmith shows the trace with load_icp/resolve_entity/plan/search/synthesize/validate/score/persist spans and `tenant_hash` metadata; LLM calls, searches and sources stayed within caps; the run cost is recorded in the build log

### Definition of Done
- [x] All gates pass (calibration set not run, non-gating); Verification Report (Pass/Fail) output
- [x] Docstring rule satisfied in every new file
- [x] `docs/build-logs/2026-11-20-day-05.md` written; README progress table updated (Gate 3 reported as blocked)

## Open Questions (only what needs the owner)
1. **Gate 3 credentials and model:** which LLM provider/model, and do you have Tavily and LangSmith keys? Without them Gate 3 is blocked.
2. **Live test companies:** which real HubSpot consultancy should the judgement check use, and (optional) the ~10 labelled companies for the calibration set?

*Resolved in review:* quote verification (yes), list endpoint (included), no-ICP behaviour (fail fast), evaluation history (kept), `JobContext` (amend Spec 003), trace privacy (hashed tenant id).

## Implementation Notes (deviations and caveats)
- **Retry placement:** retries live in the graph's node layer (`call_with_retries`), not inside the adapters, so each run's budget counters stay per-run (adapters are shared across jobs). Behaviour and tests match the spec.
- **`official_domain`** exists in `ResearchFindings` but is informational only; entity confirmation never trusts the model's domain claim.
- **Unverified fields:** `industry` and `signals` are LLM text without quote verification (length-capped); only `employee_count` and the criterion verdicts are evidence-backed.
- **Live findings (Gate 3):** OpenAI's strict structured-output mode rejected the dict-keyed `ResearchFindings` schema (`OpenAIInvalidRequestError`); `LangChainLLM` now uses `method="function_calling"` for `openai:` models only (regression test added). Tavily and the OpenAI adapter now verified live with `openai:gpt-5.4`.
- **Gate 3 status:** run live on 2026-11-22; see `docs/build-logs/2026-11-22-day-06.md`. Calibration set not run (no labelled file). Judgement-check companies were chosen by the assistant (The Middle Six; Independent Baking Co.), not the owner.
- **Operational:** provider and LangSmith keys are read from `os.environ`; a shell-exported `LANGCHAIN_TRACING_V2=false` silently disables tracing even with `LANGSMITH_TRACING=true`. Start the worker with it unset.