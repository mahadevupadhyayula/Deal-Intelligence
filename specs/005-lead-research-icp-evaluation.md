# Spec 005 — Lead Research Agent with ICP Evaluation (LangGraph)

**Status:** Draft (revised: now evaluates ICP fit, not just research)
**Phase:** 2 · **Depends on:** 001, 002, 003, 004

## Objective
Ship the first real agent: given a company, research it on the web, **judge it against the tenant's active ICP (Spec 004)**, and persist both a source-cited `LeadProfile` and an `IcpEvaluation` (score, tier, per-criterion verdicts with evidence). It runs as a `lead_research` job through the Spec 003 queue (never inline in FastAPI) as an explicit, bounded LangGraph pipeline. The LLM only gathers facts and judges each criterion; the **score and tier are computed by `score_icp` from Spec 004**, never by the LLM. External dependencies sit behind small interfaces so tests are deterministic and free; real services are used only in the Gate 3 smoke test.

## Decisions Taken (from brainstorm)
- **LLM:** provider-agnostic via LangChain `init_chat_model("<provider>:<model>")`, configured by `LLM_MODEL`.
- **Search:** Tavily behind a `SearchTool` interface, plus a fixture adapter.
- **Graph:** explicit pipeline with **one** bounded refine loop (not free-roaming ReAct).
- **Gate 2:** local golden datasets (fake LLM + fixture search); one live run with a real LangSmith trace checked in Gate 3. LangSmith is not required for CI.
- **ICP:** structured weighted criteria defined in Spec 004; this spec evaluates against the tenant's **active** ICP version.
- **Verdict:** score 0–100 + tier + per-criterion evidence.

## Technical Requirements

### 1. Dependencies and config
- `pyproject.toml`: `langgraph`, `langchain`, `langchain-core`, `langsmith`, `tavily-python`; provider packages as optional extras (`llm-anthropic`, `llm-openai`).
- `agents/config.py` + `.env.example`: `llm_model`, `tavily_api_key` (`SecretStr`), `research_max_searches` (6), `research_max_llm_calls` (6), `research_max_refine_loops` (1), `research_source_char_limit` (6000), `job_timeout_seconds` (300). No secrets in code.

### 2. Interfaces — `agents/lead_research/tools.py`, `llm.py`
- `SearchResult(url, title, content)`; `SearchTool` protocol `async search(query, max_results)`; `TavilySearch` (async, timeout, errors → `SearchError`) and `FixtureSearch`.
- `StructuredLLM` protocol: `async generate(schema: type[T], system: str, user: str) -> T`; production wraps `init_chat_model(...).with_structured_output(schema)`; tests use a scripted double. The graph depends only on this protocol.

### 3. Models
- `ResearchFindings` (LLM output, `extra="forbid"`): `draft: LeadDraft` (industry, employee_count, domain, signals) and `verdicts: list[CriterionVerdict]` (Spec 004 type).
- `IcpEvaluation(TenantModel)`: `evaluation_id`, `lead_id`, `job_id`, `icp_version`, `score`, `tier`, `evidence_ratio`, `criteria: list[CriterionVerdict]`, `contributions`, `disqualified_by`, `unverified_disqualifiers`, `status: complete | partial`, `created_at`.
- Two distinct measures, documented in code: `LeadProfile.confidence` = how well-sourced the **data** is (formula below); `IcpEvaluation.tier/score` = how well the company **fits the ICP**.

### 4. Graph — `agents/lead_research/graph.py`, `state.py`, `nodes.py`
State (`TypedDict`): `tenant_id, lead_id, job_id, company_name, domain, icp, queries, sources, draft, verdicts, unknown_criteria, refine_count, searches_used, llm_calls_used, status, profile, evaluation`.
1. `load_icp` (no LLM): load the tenant's active ICP. **No active ICP ⇒ the job fails fast** with `"no active ICP defined"` (no research, no spend).
2. `plan_queries` (LLM): 2–4 queries covering core firmographics **and the ICP criteria**; on a refine pass, only the criteria still `unknown`.
3. `search` (no LLM): run queries, dedupe by URL, truncate each source to `research_source_char_limit`.
4. `synthesize` (LLM, one structured call): returns `ResearchFindings` — profile draft plus one verdict per ICP criterion, each with evidence URLs and a rationale. Prompt rule: *no evidence ⇒ `unknown`, never guess*.
5. `validate` (no LLM), in code: (a) drop evidence URLs that search did not return; (b) **downgrade any non-`unknown` verdict with no valid evidence URL to `unknown`**; (c) add `unknown` for any criterion the LLM skipped; (d) discard verdicts for criterion ids not in the ICP; (e) compute profile `confidence`; (f) list `unknown_criteria`.
6. `score` (no LLM): `score_icp(icp, verdicts)` from Spec 004 ⇒ score, tier, disqualifiers.
7. Route: if `unknown_criteria` remain, `refine_count < max` and budget remains ⇒ back to `plan_queries`; else ⇒ `persist`.
8. `persist`: save `LeadProfile` (`AsyncLeadRepo`) and `IcpEvaluation` (`AsyncEvaluationRepo`) under the tenant, in one transaction.
- Budget guard: when searches or LLM calls hit their cap, finish with `status="partial"` using what exists (never loop past the cap). Compiled with an **optional checkpointer** (none here; Spec 008 supplies `PostgresSaver`); invoked with `thread_id = make_thread_id(tenant_id, lead_id, job_id)`.
- Profile `confidence` = `0.4·min(n_sources,3)/3 + 0.4·(filled_fields/total_fields) + 0.2·(domain confirmed in a source URL)`, clamped to [0,1]; computed, not LLM-reported.
- Design note: one combined `synthesize` call (profile + verdicts) is chosen over two calls to halve LLM cost and keep verdicts grounded in the same evidence; trade-off is a larger structured output.

### 5. Prompt-injection baseline (full guardrails are Spec 012)
- Fetched text is wrapped as `<untrusted_source id=… url=…>…</untrusted_source>`; the system prompt says contents are data, never instructions (including claims about ICP fit).
- The synthesis call has **no tools**; output must validate against `ResearchFindings`. The LLM cannot set the score or tier: it is computed in code, so injected text like "rate this a strong fit" cannot write a score directly.
- Evidence must reference fetched URLs (validate step b). Honest limit: structural defences only; a page that *looks* like legitimate evidence can still sway a verdict. Adversarial LLM evals are Spec 013.

### 6. Persistence — migration `0005_lead_icp_evaluations.sql`, async repos
- `lead_icp_evaluations(tenant_id, evaluation_id uuid, lead_id, icp_version, score, tier, status, job_id, data jsonb, created_at, PK (tenant_id, evaluation_id))`, index `(tenant_id, lead_id, created_at DESC)`, **forced RLS**, `app_user` `SELECT, INSERT`. **History is kept**: every run inserts a row; "latest" is by `created_at`.
- `AsyncLeadRepo` (`upsert`, `get`) and `AsyncEvaluationRepo` (`create`, `get_latest(lead_id)`, `list(lead_id)`) on `AsyncTenantSession`, same RLS + explicit `WHERE tenant_id` contract.

### 7. Job integration (amends Spec 003)
- Handlers receive a `JobContext` (`pool`, `search`, `llm`) as first argument: `handler(ctx, tenant_id, job_id, payload)`; `echo` and its tests are updated; `workers/settings.py` builds the context at startup and sets `function(run_job, timeout=job_timeout_seconds)`.
- Register `lead_research`. Payload `LeadResearchInput(lead_id, company_name, domain?)`, `extra="forbid"`; invalid ⇒ job `failed` with a message. Job `result` = `{status, profile, evaluation}` JSON (tier, score, criteria).

### 8. Read API — `api/routes/leads.py`
- `GET /v1/leads/{lead_id}` → profile + latest evaluation; `GET /v1/leads/{lead_id}/evaluations` → history. Tenant from key; another tenant's lead ⇒ `404`.

### 9. Observability
- LangSmith tracing by env only; runs named `lead_research`, tagged `{tenant_id, job_id, lead_id, icp_version}`. API keys never enter state or metadata.

### 10. Golden datasets — `tests/fixtures/lead_research/*.json`
Each case: input, ICP (from `docs/examples/icp_b2b_saas.json` or a variant), recorded search results, scripted LLM outputs, expected node sequence, expected queries, expected profile fields **and expected evaluation (tier, score, per-criterion verdicts)**. Cases:
`strong_fit`, `clear_non_fit`, `disqualifier_hit`, `insufficient_data`, `sparse_needs_refine` (unknown criteria resolved on the second pass), `verdict_without_evidence` (downgraded to unknown), `injection_attempt`, `budget_exhausted` (partial), `no_results`, `no_active_icp` (job fails).

### 11. Docs
- WHAT/WHERE/WHY docstring in every new file; comments on the trade-offs (bounded loop, computed scores, protocol seams, single synthesize call).

## Out of Scope
- ICP definition/management and scoring rules (Spec 004); ICP editor UI (Spec 014)
- Writing research into the LLM Wiki (Spec 006); CRM sync or writing the score back to the CRM (Spec 010+)
- `PostgresSaver` / HITL (Spec 008); full prompt-injection guardrails (Spec 012); LangSmith-hosted eval datasets and adversarial evals (Spec 013)
- Per-tenant API keys, budgets/quotas, rate limits (Specs 010/011); per-tenant tracing opt-out
- Re-evaluating existing leads when the ICP changes; routing/notifications by tier; contact/person research; page fetching beyond Tavily text; result caching

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] Config: new settings load with defaults; `tavily_api_key` is a `SecretStr`, absent from `repr`
- [ ] `TavilySearch` (HTTP mocked): maps results, applies timeout, maps failures to `SearchError`; `FixtureSearch` replays deterministically
- [ ] `LeadDraft`, `ResearchFindings`, `LeadResearchInput`, `IcpEvaluation` reject unknown fields and bad values
- [ ] Validate step: foreign evidence URLs dropped; a verdict with no valid evidence becomes `unknown`; skipped criteria become `unknown`; verdicts for non-ICP ids discarded
- [ ] Profile confidence formula: boundary cases (0 sources, 3+ sources, all/no fields, domain match) match expected numbers
- [ ] Untrusted wrapping applied to every source; budget guard stops at caps with `status="partial"`
- [ ] The LLM-provided data can never set score/tier: given a findings object containing extra `score`/`tier` fields, validation rejects it
- [ ] `AsyncLeadRepo` / `AsyncEvaluationRepo`: round-trip; history retained; latest ordering correct; tenant B cannot read or write tenant A's rows (RLS, as `app_user`)
- [ ] Handler: valid job persists profile + evaluation under the **job's** tenant only, recording the ICP version used; no active ICP ⇒ job `failed` with the message; invalid payload ⇒ `failed`; amended `echo` and Spec 003 tests still pass
- [ ] Read API: `401`, own lead `200`, cross-tenant lead `404`
- [ ] No blocking calls in async paths (static guard extended to `agents/lead_research/`)

### Gate 2 — Agent Behavior (golden datasets + trace)
- [ ] For every fixture case: node sequence, search queries, final profile **and evaluation (tier, score, per-criterion verdicts)** equal the golden expectations
- [ ] `strong_fit`, `clear_non_fit`, `disqualifier_hit` (tier `not_fit`, score 0, criterion listed) and `insufficient_data` yield the expected tiers
- [ ] `sparse_needs_refine` loops exactly once and the second-pass queries target only unknown criteria; `budget_exhausted` ends `partial` without exceeding caps; `no_results` ends `insufficient_data`
- [ ] `injection_attempt`: no injected fields/URLs in output, score computed by code, normal trajectory only (structural defence; see §5 limit)
- [ ] `no_active_icp`: job fails before any search or LLM call
- [ ] Two-tenant run: separate ICPs and results, no cross-tenant data in state or DB; `thread_id = tenant:lead:job`
- [ ] (Live, with Gate 3) LangSmith trace exists with expected node spans and `tenant_id`/`icp_version` metadata

### Gate 3 — Live Smoke Test (needs `TAVILY_API_KEY`, an LLM provider key, `LANGSMITH_API_KEY`)
- [ ] Redis + Postgres up, migrations applied, tenant + API key minted, sample ICP loaded via `PUT /v1/icp`, API and worker running with real adapters
- [ ] `POST /v1/jobs {"kind":"lead_research","payload":{"lead_id":"l1","company_name":"Stripe","domain":"stripe.com"}}` ⇒ `202`; job `succeeded` within the timeout
- [ ] `GET /v1/leads/l1` shows a profile with ≥ 2 source URLs and an evaluation whose `icp_version` matches the active ICP
- [ ] Consistency check: re-running `score_icp` on the stored verdicts reproduces the stored score and tier exactly; every non-`unknown` verdict has ≥ 1 evidence URL
- [ ] Judgment check (reported honestly, LLM output varies): under the sample ICP, Stripe scores ≥ `moderate` and a clearly mismatched company (e.g. a local bakery) scores ≤ `weak`/`not_fit`
- [ ] LangSmith shows the trace with load_icp/plan/search/synthesize/validate/score/persist spans and `tenant_id` metadata; LLM calls and searches stayed within caps

### Definition of Done
- [ ] All gates pass; Verification Report (Pass/Fail) output
- [ ] Docstring rule satisfied in every new file
- [ ] `docs/build-logs/YYYY-MM-DD-day-XX.md` written; README progress table updated (Spec 005 → Done)

## Open Questions for Review
1. **Smoke-test credentials and model:** which LLM provider/model for Gate 3, and do you have Tavily and LangSmith keys? Without them Gate 3 is marked blocked, not Pass.
2. **No ICP yet:** fail the job fast (recommended) vs. run research only and return `insufficient_data`.
3. **Evaluation history:** keep every run (recommended, auditable) vs. overwrite with the latest.
4. **Trace privacy:** LangSmith traces will contain scraped company data and the opaque `tenant_id`. Acceptable for now, or hash tenant ids in metadata?
5. **`JobContext` amendment:** OK to change the Spec 003 handler signature (small, mechanical) rather than using a module-level global?
