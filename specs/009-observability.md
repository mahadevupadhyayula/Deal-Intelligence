# Spec 009 — Observability: Structured Logs, Metrics, Tracing Wiring

**Status:** Done; Gates 1, 2 (N/A, equivalence evidence) and 3 pass
**Phase:** Slice 1 (Lead Research) — Deploy: local · **Depends on:** 003, 006, 007, 008

## Objective
Make the running stack explain itself. Today the API and worker emit unstructured log lines, expose no metrics, and a failed job can only be diagnosed by reading Postgres. This spec adds (1) JSON logs with a per-job correlation id and a hashed tenant id, (2) Prometheus metrics for queue depth, job latency/outcome, provider calls and estimated LLM cost, (3) a deliberate LangSmith tracing configuration that links a trace to its job, and (4) an optional local Prometheus + Grafana profile so the signals can be seen. It is the instrumentation that Spec 010 (KEDA scales on queue depth) and the Streamlit UI (Spec 012) will read. No agent behaviour changes.

## Decisions Taken
- **Metrics are never labelled by tenant.** `tenant_id` as a Prometheus label is unbounded cardinality (cost, performance) and exposes the tenant list to anyone who can scrape. Per-tenant cost already lives in `tenant_usage` (Spec 006); metrics are fleet-level only. Labels are limited to small closed sets: `kind`, `status`, `provider`, `reason`.
- **Logs identify tenants by a keyed hash**, not the raw id: `tenant = HMAC-SHA256(LOG_HASH_KEY, tenant_id)[:12]`. Stable for correlating one tenant's events, not reversible from the logs. The same hash is used in LangSmith metadata (ARCHITECTURE.md already promises "hashed tenant ids in traces"). If `LOG_HASH_KEY` is unset the process generates a random per-process key and logs one warning (hash is then only stable within a process). Never hardcoded.
- **Correlation is the `job_id`.** It already travels API -> Postgres -> ARQ message -> worker, so no new id format is invented. Logs carry `job_id`, `kind`, `tenant` via `contextvars`, set at the top of the request handler and of `run_job`, so every line emitted underneath (including from libraries) is correlated without passing arguments around. The API also honours/echoes an `X-Request-ID` header for non-job requests.
- **Standard-library logging, not a new framework.** A small `JsonFormatter` on the existing `logging` module, with redaction (Spec 006) applied first, so the existing `install_log_redaction` still guarantees no secret shapes reach output. Trade-off: no `structlog` dependency or processors chain; we lose some ergonomics but keep one logging system that LangChain/uvicorn/arq already use. Format is `LOG_FORMAT=json|text` (default `json` in containers, `text` for local dev).
- **No raw payloads in logs.** Log job ids, kinds, status transitions, durations, counts and error *types*. Job payloads, lead names, prompts and scraped text are never logged (they are tenant data and can contain PII); redacted error text may be logged because it is already what we store.
- **`prometheus_client`, pull model.** It is dependency-light, matches Kubernetes/KEDA conventions, and needs no collector. OpenTelemetry (OTLP export) is deferred: it adds an SDK, a collector and a backend for no capability the metrics plus LangSmith do not already give at this stage (see Out of Scope).
- **Queue depth is measured from Redis, not from workers.** ARQ keeps queued jobs in the sorted set `arq:queue`; `ZCARD` is the backlog, the same number KEDA's Redis scaler will read in Spec 010. The API exposes it as a gauge computed at scrape time (one cheap Redis call, with a short timeout; on failure the gauge is omitted and `di_scrape_errors_total` increments, never a 500). Oldest-job age (`ZRANGE ... WITHSCORES` first element) is exposed too: depth alone cannot tell "busy" from "stuck".
- **Worker metrics via a tiny HTTP listener.** ARQ workers have no HTTP server, so `startup` starts `prometheus_client.start_http_server` on `WORKER_METRICS_PORT` (default 9100). Each scaled worker replica exposes its own counters; Prometheus sums across replicas. Counters reset on restart, which is normal for Prometheus (`rate()` handles it).
- **`/metrics` is not a public endpoint.** The API serves it on a **separate port** (`API_METRICS_PORT`, default 9101) via a second listener, not on the tenant-facing port, so the public ingress (Spec 010/011) can never route to it and no auth scheme is needed. Compose publishes neither metrics port to the host by default (Prometheus reaches them on the compose network).
- **LLM cost is an estimate and says so.** Adapters do not surface token counts today. Metrics count calls (`di_provider_calls_total{provider}`) and cost is `calls x configured price` using the same two settings as the eval harness (promoted from `EVAL_PRICE_*` to shared `PRICE_PER_LLM_CALL` / `PRICE_PER_SEARCH`, with the old names still read as fallback). Real token-based cost is a clearly-marked follow-up (see Open Questions).
- **LangSmith: explicit, redacted, correlated.** Tracing stays opt-in via environment (`LANGSMITH_TRACING`, `LANGSMITH_PROJECT`, key in the worker only, as in Spec 008). Added: run metadata `{job_id, kind, tenant (hashed), app_env}` and tags on every traced run, so a trace is found from a job id and vice versa; the job result/log carries the trace id when tracing is on. Trace redaction (Spec 006) is unchanged and is re-asserted by a test.
- **Observability must not hurt the job path.** Metric updates are in-process counter increments (no I/O). A failure inside instrumentation is swallowed and counted, never raised into `run_job` or a request. Log/metric code has no database access.

## Technical Requirements

### 1. Files
- `agents/observability/__init__.py`
- `agents/observability/logging.py` — `JsonFormatter`, `configure_logging(service)`, `bind_context(**fields)` / `clear_context()` (contextvars), `hash_tenant(tenant_id)`. Idempotent; composes with `install_log_redaction`.
- `agents/observability/metrics.py` — metric definitions (single module so names/labels live in one place) and thin helpers (`observe_job(kind, status, seconds)`, `count_provider_call(provider)`, `count_scrape_error(reason)`).
- `agents/observability/queue.py` — `async def queue_stats(redis) -> QueueStats(depth, oldest_age_seconds)`; used by the API scrape collector and by the smoke test.
- `agents/observability/tracing.py` — `run_metadata(job_id, kind, tenant)`; applied where the LangGraph run config is built.
- `api/main.py` — call `configure_logging("api")`, add request middleware (binds `request_id`, `tenant` when authenticated, logs one access line with method, route template, status, duration; **no query string, no body**), start the metrics listener in `lifespan`.
- `workers/settings.py` — `configure_logging("worker")`, start the metrics listener in `startup`.
- `workers/tasks.py` — bind `job_id/kind/tenant` for the duration of `run_job`; log transitions (`job.started`, `job.finished` with `status`, `duration_ms`, `llm_calls`, `searches`, `error_type`); record `di_job_duration_seconds`, `di_jobs_total`, `di_provider_calls_total`, `di_job_queue_wait_seconds` (started minus created). Instrumentation wraps, and never changes, the Spec 008 cancel handling.
- `agents/lead_research/graph` entry (where the run `config` with `thread_id` is built) — attach `run_metadata` to config `metadata`/`tags`.
- `agents/config.py` + `.env.example` — `LOG_FORMAT`, `LOG_LEVEL`, `LOG_HASH_KEY` (secret, documented as such), `WORKER_METRICS_PORT`, `API_METRICS_PORT`, `METRICS_ENABLED`, `PRICE_PER_LLM_CALL`, `PRICE_PER_SEARCH`.
- `infra/observability/prometheus.yml`, `infra/observability/grafana/` (provisioned datasource + one dashboard JSON) — local only.
- `docker-compose.yml` — new profile `observability`: `prometheus` and `grafana` (pinned images, localhost-only ports, read-only config mounts, Grafana admin password from `GRAFANA_ADMIN_PASSWORD` required via `${VAR:?}`, anonymous access off). Core services get the new env vars and the metrics ports are `expose`d (not published). Scaled workers are discovered by Docker DNS service name (`tasks.worker` style A-record lookup in Prometheus `dns_sd_configs`).
- `docs/local-stack.md` — add an Observability section (how to enable, what each metric means, example queries, how to find a job's logs and trace).
- `pyproject.toml` / `requirements.lock` — add `prometheus-client`; regenerate the lock (reviewed step, as in Spec 008).
- `scripts/observability_smoke.sh` — Gate 3 script (below).

### 2. Metrics catalogue (all prefixed `di_`)
| Metric | Type | Labels | Where | Meaning |
|---|---|---|---|---|
| `di_queue_depth` | gauge | — | API scrape | jobs waiting in `arq:queue` |
| `di_queue_oldest_age_seconds` | gauge | — | API scrape | age of the oldest waiting job |
| `di_jobs_total` | counter | `kind`, `status` (`succeeded`/`failed`/`cancelled`) | worker | finished jobs |
| `di_job_duration_seconds` | histogram | `kind` | worker | handler wall time (buckets suited to 1s–300s) |
| `di_job_queue_wait_seconds` | histogram | `kind` | worker | time from creation to start |
| `di_provider_calls_total` | counter | `provider` (`llm`/`search`) | worker | metered calls (retries and failures included) |
| `di_estimated_cost_usd_total` | counter | `provider` | worker | calls x configured price |
| `di_admission_rejections_total` | counter | `reason` (`rate`/`active_cap`/`daily_quota`/`payload`) | API | 429/413 returned at admission |
| `di_http_requests_total` / `di_http_request_duration_seconds` | counter / histogram | `method`, `route`, `status_class` | API | `route` is the template (`/v1/jobs/{job_id}`), never the raw path |
| `di_scrape_errors_total` | counter | `reason` | API/worker | instrumentation itself failed (queue read, etc.) |
| `di_worker_up` | gauge | — | worker | 1 while the process is alive; lets alerts distinguish "no jobs" from "no workers" |

Cardinality rule, enforced by a test: every label value comes from a closed set; no metric has a label named `tenant`, `tenant_id`, `job_id`, `user` or `path`.

### 3. Log schema
One JSON object per line: `ts` (UTC ISO-8601), `level`, `service` (`api`/`worker`), `logger`, `msg`, plus bound context (`job_id`, `kind`, `tenant` hashed, `request_id`) and event fields (`status`, `duration_ms`, `error_type`). Exceptions are logged as a redacted, truncated `exc` string (no local variables). Tests parse the output as JSON.

### 4. Constraints from CLAUDE.md
- **Never block FastAPI:** middleware does O(1) work; the queue gauge is read at scrape time on the metrics listener, never in a request handler, with a short timeout.
- **`tenant_id` everywhere:** nothing here adds a query path. Logs and traces carry only the hashed tenant; `queue_stats` reads Redis keys that hold no tenant data (messages carry ids only, Spec 003).
- **Stateless:** all metric state is per-process and disposable; nothing is required to survive a restart.
- **Zero hardcoded secrets:** `LOG_HASH_KEY`, `GRAFANA_ADMIN_PASSWORD` and `LANGSMITH_API_KEY` come from the environment; the compose test asserts no literal values and that `LANGSMITH_API_KEY` reaches only `worker`/`tools`.
- **Commenting rule:** every new file has the WHAT/WHERE/WHY docstring; the cardinality and hashing trade-offs are commented where they are implemented.

## Out of Scope
- OpenTelemetry SDK/OTLP exporters, distributed spans across API->worker, and a collector (revisit with Spec 011 if the cloud target needs CloudWatch/X-Ray; the log `job_id` already gives cross-service correlation).
- Alert rules and paging (a recommended rule set is documented in `docs/local-stack.md`, not enforced); on-call tooling.
- Token-level LLM cost (needs adapter changes; see Open Questions).
- Per-tenant dashboards or usage APIs (Spec 012 UI reads `tenant_usage`).
- The stale-`running` job reaper and graceful drain (Spec 010); this spec adds the metric (`di_jobs_total{status="cancelled"}`) that makes them visible.
- Log shipping/aggregation (Loki, CloudWatch): stdout JSON is the contract; Spec 010/011 choose the shipper.
- Kubernetes ServiceMonitor/Prometheus-operator manifests (Spec 010).

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] `JsonFormatter`: output is valid JSON with the required keys; bound context appears on every line within the context and not after `clear_context()`; context does not leak between concurrent asyncio tasks.
- [ ] Redaction still applies: a record containing an `sk-...`/DB URL/registered secret is emitted redacted in JSON output; emails/phones in logged messages are not added by us (we never log payloads).
- [ ] `hash_tenant`: deterministic for a key, differs across tenants and across keys, 12 chars, never contains the raw id; missing key -> random per-process key + one warning.
- [ ] Metric cardinality test: no forbidden label names; every labelled metric rejects an out-of-set value in the helper (unknown `kind`/`reason` maps to `other`).
- [ ] `run_job` records `di_jobs_total` / duration for succeeded, failed, timed-out and **cancelled** jobs, and the Spec 008 cancel test still passes unchanged.
- [ ] Instrumentation failure (patched to raise) does not change the job outcome or raise out of `run_job` or a request.
- [ ] `queue_stats` against real Redis: depth and oldest-age for 0, 1 and N queued jobs; Redis down -> `None` and a counted scrape error, no exception.
- [ ] Admission rejections increment the right `reason` (429 rate / active cap / quota, 413 payload).
- [ ] HTTP middleware logs the route template, not the raw path or query string; an authenticated request logs the hashed tenant, never the API key or tenant id.
- [ ] Static compose test: `observability` profile exists, metrics ports are not published, `GRAFANA_ADMIN_PASSWORD` is `${VAR:?}`, `LANGSMITH_API_KEY` only in worker/tools, `LOG_HASH_KEY` not hardcoded.
- [ ] `tenant_id` isolation tests unchanged and green (no new query paths); a test asserts the log/metric/trace surfaces never contain the raw test tenant id.

### Gate 2 — Agent Behavior
**N/A with reason:** no agent logic, prompts, tools or graph topology change; instrumentation wraps the existing run. Equivalence evidence: the 22 golden cases still pass (scripted) and the Spec 005/007 trajectories are unchanged. One added check: the run `config` passed to the graph now carries `metadata`/`tags`, and a test asserts the golden trajectories are identical with and without them.

### Gate 3 — Live Smoke Test (`scripts/observability_smoke.sh`, free `echo` jobs only; no provider spend)
- [ ] `docker compose --profile observability up -d --build --wait` -> api, worker, prometheus, grafana healthy; metrics ports not reachable from the host.
- [ ] Submit one `echo` job: worker logs contain `job.started` and `job.finished` as valid JSON sharing the submitted `job_id`; no raw `tenant_id` and no key material appear anywhere in `docker compose logs`.
- [ ] Stop the worker, submit 5 jobs (with `delay_seconds`): `di_queue_depth == 5` and `di_queue_oldest_age_seconds` increases between two scrapes; start the worker: depth returns to 0 and `di_jobs_total{kind="echo",status="succeeded"}` rises by 5. (Rehearses exactly what KEDA will read in Spec 010.)
- [ ] Prometheus has both targets `up` (api and the scaled worker replicas) and answers a `sum(di_queue_depth)` query.
- [ ] Trigger each admission rejection once: `di_admission_rejections_total` shows each reason.
- [ ] `docker compose stop worker` mid-job: `di_jobs_total{status="cancelled"}` increments (the Spec 008 behaviour is now observable).
- [ ] Grafana provisions the datasource and dashboard (HTTP check against its API with the admin credential from the environment; value never printed).
- [ ] Forged-secret check: run a job whose payload contains a fake `sk-` string; it never appears in any log line.
- [ ] Optional, only if the user opts in: one real `lead_research` run with `LANGSMITH_TRACING=true` to confirm the trace carries `job_id`/`tenant` metadata and redacted content (counts toward spend; not part of the default gate).

### Definition of Done
- [ ] All gates pass; Pass/Fail Verification Report output before the build log
- [ ] Every file has WHAT/WHERE/WHY docstring
- [ ] Build log written; README progress table updated (009 -> Done)
- [ ] `docs/local-stack.md` and `docs/ARCHITECTURE.md` updated (metric catalogue, log schema, how to correlate a job)

## Open Questions (need your decision before approval)
1. **Prometheus + Grafana locally?** Recommended: yes, as an opt-in `observability` compose profile (a few hundred MB of images, off by default). Alternative: ship only the metrics and log format now and defer the dashboards to Spec 010's kind cluster.
2. **Metrics on a separate port (recommended) vs a `/metrics` route on the public API port?** Separate port keeps it off the public ingress with no auth scheme; the route is simpler but needs a token or network policy later.
3. **LLM cost: calls x flat price now (recommended), or capture real token usage?** Tokens are more accurate but need changes to the LLM adapter and the golden fixtures, and provider usage fields differ. I suggest a small follow-up spec after the UI exists.
4. **OpenTelemetry now or deferred?** Recommended deferred (see Out of Scope): with one API, one worker and `job_id` correlation it adds weight without new insight; revisit for the AWS target.
5. **Live LangSmith confirmation:** include one paid real run in Gate 3 (cents), or rely on a unit-level check of the metadata and skip it? Recommended: include it once, with your go-ahead at that point.

## Resolved Questions
All five recommendations were approved: (1) Prometheus + Grafana as an opt-in profile; (2) metrics on separate internal ports; (3) cost = calls x flat price; (4) OpenTelemetry deferred; (5) live LangSmith run only with a later go-ahead (not run in this spec).

## Implementation Notes (deviations and findings)
- **Queue depth semantics.** ARQ leaves a job in `arq:queue` until it *finishes*, so `ZCARD` = waiting + running. `queue_stats` subtracts `arq:in-progress:<id>` markers and reports `di_queue_depth` (waiting), `di_queue_in_flight` (running) and `di_queue_oldest_age_seconds` (oldest *waiting*). Spec 010 can scale on waiting or on the total. `queue_stats` is **synchronous** (not `async` as drafted) because the collector runs on the metrics listener thread with a short-timeout sync Redis client, which keeps a slow Redis off the FastAPI event loop.
- **`GRAFANA_ADMIN_PASSWORD` is not `${VAR:?}`.** Compose interpolates inactive profiles too, so `:?` would break `docker compose up` for everyone not using observability (verified). The Grafana entrypoint refuses to start with an empty password instead. `LOG_HASH_KEY` *is* `:?` (needed by api and worker) and was appended to the git-ignored `.env` with `GRAFANA_ADMIN_PASSWORD` (values never printed).
- **Counters die with the process.** A worker stopped by SIGTERM increments `di_jobs_total{status="cancelled"}` and then exits; Prometheus only sees it if a scrape lands in between. Gate 3 therefore asserts the `job.finished status=cancelled` **log line**; the unit test asserts the counter. A push gateway/OTel is out of scope.
- **LangSmith metadata keeps the key `tenant_hash`** (existing test and consumers) and now uses the shared keyed hash; tags are `[kind, "job:<id>"]`; when tracing is enabled the handler sets its own run id and logs `trace.linked`.
- **Prices:** new `PRICE_PER_LLM_CALL` / `PRICE_PER_SEARCH`, with `EVAL_PRICE_*` accepted as fallback names; the eval harness still reads its own fields.
- **Library loggers:** `configure_logging` routes uvicorn/arq loggers through the JSON handler; the API runs with `--no-access-log` because the app's middleware writes the structured access line. Probes (`/healthz`, `/readyz`) log at DEBUG.
- **Smoke-script findings (test bugs, not product bugs):** the first run showed rate-limit counters are per tenant per minute and shared with earlier steps (step 4 now uses a fresh tenant), `docker ps` lists `expose`d ports without a host mapping (check `:910x->`), and mval normalises `5.0` to `5`.
- **Known limits:** the fixed-window rate limiter can straddle a minute boundary, so the `rate` assertion is `>= 1`. Real token-based cost, OpenTelemetry, alert rules and log shipping remain out of scope as drafted.

- **Superseded by Spec 010:** the worker now drains on SIGTERM, so `scripts/observability_smoke.sh` step 6 asserts a mid-job stop *succeeds* (and logs `job.finished status=succeeded`); the `cancelled` path is still covered by the unit test.
