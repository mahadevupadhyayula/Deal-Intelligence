# Spec 004 — Lead Research Agent (LangGraph)

**Status:** Draft
**Phase:** 2 · **Depends on:** 001, 002, 003

## Objective
Ship the first real agent: given a company, research it on the web and produce a validated, source-cited `LeadProfile` saved for the caller's tenant. It runs as a `lead_research` job through the Spec 003 queue (never inline in FastAPI) as an explicit, bounded LangGraph pipeline. All external dependencies (LLM, search) sit behind small interfaces so tests are deterministic and free; real services are exercised only in the Gate 3 smoke test.

## Decisions Taken (from brainstorm)
- **LLM:** provider-agnostic via LangChain `init_chat_model("<provider>:<model>")`, configured by `LLM_MODEL`; no provider is hard-coded.
- **Search:** Tavily behind a `SearchTool` interface, plus a fixture adapter.
- **Graph:** explicit pipeline with **one** bounded refine loop (not a free-roaming ReAct agent).
- **Gate 2:** local golden datasets (fake LLM + fixture search) assert trajectory and output; one live run with a real LangSmith trace is checked in Gate 3. LangSmith is not required for CI.

## Technical Requirements

### 1. Dependencies and config
- `pyproject.toml`: `langgraph`, `langchain`, `langchain-core`, `langsmith`, `tavily-python`. Provider packages are optional extras (`llm-anthropic`, `llm-openai`) so deployments install only what they use.
- `agents/config.py` + `.env.example`: `llm_model` (e.g. `anthropic:claude-sonnet-5-5`), `tavily_api_key` (`SecretStr`), `research_max_searches` (default 6), `research_max_llm_calls` (default 6), `research_max_refine_loops` (default 1), `research_source_char_limit` (default 6000), `job_timeout_seconds` (default 300). No secrets in code.

### 2. Interfaces — `agents/lead_research/tools.py`, `llm.py`
- `SearchResult(url, title, content)`; `SearchTool` protocol: `async search(query, max_results) -> list[SearchResult]`.
- `TavilySearch` (adapter, async, timeout, errors mapped to `SearchError`) and `FixtureSearch` (replays recorded results by query).
- `StructuredLLM` protocol: `async generate(schema: type[T], system: str, user: str) -> T`. Production impl wraps `init_chat_model(...).with_structured_output(schema)`; tests use a scripted double. The graph depends only on this protocol.

### 3. Graph — `agents/lead_research/graph.py`, `state.py`, `nodes.py`
State (`TypedDict`): `tenant_id, lead_id, company_name, domain, queries, sources, draft, missing_fields, refine_count, searches_used, llm_calls_used, status, profile`.
Nodes and edges:
1. `plan_queries` (LLM): 2–4 search queries; on a refine pass, targeted at `missing_fields`.
2. `search` (no LLM): run queries, dedupe by URL, truncate each source to `research_source_char_limit`.
3. `synthesize` (LLM): structured output `LeadDraft` (industry, employee_count, domain, signals) from the wrapped sources, each claim tied to source URLs.
4. `validate` (no LLM): drop sources not actually fetched, compute `confidence` (below), list `missing_fields`.
5. Route: if `missing_fields` and `refine_count < max` and budget remains → `plan_queries`; else → `persist`.
6. `persist`: build `LeadProfile`, save via `AsyncLeadRepo` under the tenant.
- Budget guard: when searches or LLM calls hit their cap the graph finishes with `status="partial"` and what it has (never loops past the cap). Graph compiles with an **optional checkpointer** (none here; Spec 007 supplies `PostgresSaver`) and is invoked with `thread_id = make_thread_id(tenant_id, lead_id, job_id)`.
- **Confidence is computed, not LLM-reported:** `0.4·min(n_sources,3)/3 + 0.4·(filled_fields/total_fields) + 0.2·(domain confirmed in a source URL)`, clamped to [0,1]. The formula is documented in the code and unit-tested.

### 4. Prompt-injection baseline (full guardrails are Spec 011)
- Fetched text is wrapped as `<untrusted_source id=… url=…>…</untrusted_source>`; the system prompt states its contents are data, never instructions.
- The synthesis LLM call is given **no tools**; output must validate against `LeadDraft` (`extra="forbid"`), so injected text cannot trigger actions or add fields.
- `validate` keeps only source URLs that were actually returned by search (no invented citations).
- A fixture with an injection attempt is part of the golden set (Gate 2).

### 5. Job integration (amends Spec 003)
- Handlers now receive a `JobContext` (`pool`, `search: SearchTool`, `llm: StructuredLLM`) as first argument: `handler(ctx, tenant_id, job_id, payload)`. `echo` and its tests are updated; `workers/settings.py` builds the context at startup (real adapters) and `function(run_job, timeout=job_timeout_seconds)`.
- Register `lead_research` in `JOB_HANDLERS`. Payload model `LeadResearchInput(lead_id, company_name, domain?)` with `extra="forbid"`; invalid payload ⇒ job `failed` with a validation message. Job `result` is the saved `LeadProfile` JSON plus `status` (`complete|partial`).

### 6. Persistence — `agents/repos/lead_async.py`
- `AsyncLeadRepo` (`upsert`, `get`) on `AsyncTenantSession`, same RLS + explicit `WHERE tenant_id` contract as the sync `LeadRepo` (which stays).

### 7. Observability
- LangSmith tracing is enabled purely by env (`LANGSMITH_TRACING`, key). Each run is tagged with metadata `{tenant_id, job_id, lead_id}` and a `lead_research` run name. API keys are never placed in state or metadata.

### 8. Golden datasets — `tests/fixtures/lead_research/*.json`
Each case holds: input, recorded search results, scripted LLM outputs, expected node sequence, expected tool calls (queries), and expected `LeadProfile` fields. Cases: `acme_happy` (single pass), `sparse_needs_refine` (one refine loop), `injection_attempt`, `budget_exhausted` (partial), `no_results`.

### 9. Docs
- WHAT/WHERE/WHY docstring in every new file; comments on the trade-offs (bounded loop, computed confidence, protocol seams).

## Out of Scope
- Writing research into the LLM Wiki (Spec 005); CRM seeding/sync (Spec 009)
- `PostgresSaver` / HITL (Spec 007); full prompt-injection guardrails (Spec 011); LangSmith-hosted eval datasets (Spec 012)
- Per-tenant API keys, budgets/quotas and rate limits (Specs 009/010); per-tenant tracing opt-out
- Fetching pages beyond the text Tavily returns; result caching; contact/person research

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] Config: new settings load with defaults; `tavily_api_key` is a `SecretStr`, absent from `repr`
- [ ] `TavilySearch` (HTTP mocked): maps results, applies timeout, maps failures to `SearchError`; `FixtureSearch` replays deterministically
- [ ] `LeadDraft`/`LeadResearchInput` reject unknown fields and bad values
- [ ] Confidence formula: boundary cases (0 sources, 3+ sources, all/no fields, domain match) match expected numbers
- [ ] Source filtering: a source URL not returned by search is dropped; untrusted wrapping applied to every source
- [ ] Budget guard stops at the configured caps and yields `status="partial"`
- [ ] `AsyncLeadRepo`: round-trip; tenant B cannot read/overwrite tenant A's lead (RLS, as `app_user`)
- [ ] Handler: valid job persists a `LeadProfile` under the **job's** tenant only; invalid payload ⇒ `failed`; amended `echo` and Spec 003 tests still pass
- [ ] No blocking calls in async paths (existing static guard extended to `agents/lead_research/`)

### Gate 2 — Agent Behavior (golden datasets + trace)
- [ ] For every fixture case: node sequence, search queries and final `LeadProfile` equal the golden expectations
- [ ] `sparse_needs_refine` loops exactly once; `budget_exhausted` ends `partial` without exceeding caps; `no_results` ends `partial` with confidence below a defined floor
- [ ] `injection_attempt`: output contains no injected fields/URLs, and no tool call or state change occurs beyond the normal path
- [ ] Graph runs with `thread_id = tenant:lead:job`, and no cross-tenant data appears in state for a two-tenant run
- [ ] (Live, with Gate 3) LangSmith trace exists for the run with the expected node spans and `tenant_id` metadata

### Gate 3 — Live Smoke Test (needs `TAVILY_API_KEY`, an LLM provider key, `LANGSMITH_API_KEY`)
- [ ] Redis + Postgres up, `python -m agents.migrate`, tenant + API key minted, API and worker running with real adapters
- [ ] `POST /v1/jobs {"kind":"lead_research","payload":{"lead_id":"l1","company_name":"Stripe","domain":"stripe.com"}}` ⇒ `202`; the job reaches `succeeded` within the timeout
- [ ] `psql`: `SELECT data FROM leads WHERE tenant_id='…'` shows a profile with ≥ 2 source URLs and computed confidence
- [ ] LangSmith shows the trace with plan/search/synthesize/validate/persist spans and `tenant_id` metadata (checked via the `langsmith` client)
- [ ] LLM calls and searches in the run stayed within the configured caps (read from trace/state)

### Definition of Done
- [ ] All gates pass; Verification Report (Pass/Fail) output
- [ ] Docstring rule satisfied in every new file
- [ ] `docs/build-logs/YYYY-MM-DD-day-XX.md` written; README progress table updated (Spec 004 → Done)

## Open Questions for Review
1. **Smoke-test credentials and model:** which LLM provider/model will you use for Gate 3, and do you have Tavily and LangSmith keys? Without them Gate 3 cannot be run; I would then mark it as blocked rather than Pass.
2. **Trace privacy:** LangSmith traces will contain scraped company data and the opaque `tenant_id`. Acceptable for now (per-tenant opt-out deferred), or should tenant ids be hashed in metadata?
3. **`JobContext` amendment:** changing the handler signature touches Spec 003 code and tests (small, mechanical). OK to amend in this spec rather than adding a module-level global?
