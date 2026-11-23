# Local stack runbook (Docker Compose, Spec 008)

## First run
1. `cp .env.example .env` and set at least `POSTGRES_PASSWORD` and `APP_USER_PASSWORD` (URL-safe characters only: no `@ : / ? #`). For research jobs also set `LLM_MODEL`, `TAVILY_API_KEY` and the provider key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`). `.env` is git-ignored and never enters an image.
2. `docker compose up -d --build --wait` (first build about a minute). `migrate` runs once, then `api` and `worker` start.
3. Create a tenant and key (the key is printed once): `docker compose run --rm tools python -m agents.admin create-tenant acme` then `... create-api-key acme`.
4. Submit a free job: `curl -X POST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"kind":"echo","payload":{"hi":1}}'`, then `GET /v1/jobs/<id>`.

## Day to day
| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f api worker` |
| Scale workers | `docker compose up -d --scale worker=3 --wait worker` |
| Run the free golden suite in the image | `docker compose run --rm tools python -m agents.evals run golden` |
| Full smoke test (Gate 3) | `bash scripts/compose_smoke.sh` (use `--keep` to leave the stack up) |
| Stop, keep data | `docker compose down` |
| Reset everything (deletes volumes) | `docker compose down -v` |

Ports (127.0.0.1 only): API `8000`, Postgres `5433`, Redis `6380` (change with `API_HOST_PORT`, `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`; the defaults avoid clashing with a local 5432/6379).

## Who gets which secret
| Variable | migrate | api | worker | tools |
|---|---|---|---|---|
| `ADMIN_DATABASE_URL` (superuser) | yes | no | no | yes |
| `DATABASE_URL` (`app_user`, RLS applies) | yes | yes | yes | yes |
| `REDIS_URL` | no | yes | yes | yes |
| `TAVILY_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LANGSMITH_API_KEY` | no | no | yes | yes |
| `POSTGRES_PASSWORD`, `APP_USER_PASSWORD` | build the URLs above (compose interpolation); the Postgres container also receives `POSTGRES_PASSWORD` |

## Behaviour worth knowing
- **Stopping a worker cancels its in-flight job.** ARQ does not drain on SIGTERM; the job is recorded `failed: worker stopped before the job finished; resubmit` (it is no longer left `running`). A hard kill (SIGKILL/out-of-memory) cannot be caught and would leave a `running` row; a stale-job reaper is a follow-up.
- **Free slow jobs for scaling experiments:** set `ECHO_MAX_DELAY_SECONDS=20` for the worker and submit `{"kind":"echo","payload":{"delay_seconds":15}}`. Disabled (0) by default.
- **Redis uses `noeviction` and persistence:** the queue, job messages and rate-limit counters must not be evicted or lost on restart.
- The image is built from `requirements.lock` (exact versions). Regenerate deliberately: `uv pip compile pyproject.toml --extra llm-openai --extra llm-anthropic -o requirements.lock`, re-run the tests, review the diff.

## Observability (Spec 009)

Logs are one JSON object per line on stdout (`LOG_FORMAT=json` in compose). Every line inside a request or job carries `job_id`, `kind`, `request_id` and `tenant`; the tenant is a keyed hash (`HMAC(LOG_HASH_KEY, tenant_id)[:12]`), never the raw id. `LOG_HASH_KEY` is **required** and must be the same for the API and every worker (generate with `python -c "import secrets; print(secrets.token_hex(32))"`). Payloads, prompts and scraped text are never logged.

**Find everything about one job:**
```bash
docker compose logs --no-log-prefix api worker | jq -R 'fromjson? | select(.job_id=="<JOB_ID>")'
```
Lifecycle events: `http.request` (API), `job.started` (with `queue_wait_ms`), `job.finished` (`status` succeeded/failed/cancelled, `duration_ms`, `llm_calls`, `searches`, `error_type`), `trace.linked` (LangSmith run id, only when tracing is on). In LangSmith, search by tag `job:<JOB_ID>` or metadata `job_id`.

**Metrics** (Prometheus text format, internal ports only: api `9101`, worker `9100`; never published to the host):
```bash
docker compose exec -T api python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:9101/metrics').read().decode())" | grep '^di_'
```
| Metric | Meaning |
|---|---|
| `di_queue_depth` / `di_queue_in_flight` / `di_queue_oldest_age_seconds` | waiting jobs / running jobs / age of the oldest waiting job (API, read from Redis at scrape time). ARQ keeps running jobs in its queue until they finish, so we subtract in-progress markers to get the true backlog |
| `di_jobs_total{kind,status}` | finished jobs (`succeeded`, `failed`, `cancelled`) |
| `di_job_duration_seconds`, `di_job_queue_wait_seconds` | histograms by `kind` |
| `di_provider_calls_total{provider}`, `di_estimated_cost_usd_total{provider}` | metered calls and calls x `PRICE_PER_*` (an estimate; no token counts) |
| `di_admission_rejections_total{reason}` | `rate`, `active_cap`, `daily_quota`, `payload` |
| `di_http_requests_total`, `di_http_request_duration_seconds` | by method, route template, status class |
| `di_worker_up`, `di_scrape_errors_total` | worker liveness; instrumentation failures |

Metrics are fleet-level only: there is deliberately no tenant label (cardinality, and it would reveal the tenant list). Per-tenant usage is in the `tenant_usage` table.
Counters live in the process: a worker that is stopped takes its counters with it, so a `cancelled` job is visible in the `job.finished` log line and in Prometheus only if a scrape happened after the increment. Use logs for forensic questions and metrics for rates.

**Dashboards:** set `GRAFANA_ADMIN_PASSWORD` in `.env`, then
```bash
docker compose --profile observability up -d
```
Prometheus is at `http://127.0.0.1:9090`, Grafana at `http://127.0.0.1:3000` (user `admin`, anonymous access off), with the "Deal Intelligence - Overview" dashboard provisioned. Scaled workers are discovered by DNS (`--scale worker=N` just works).

**Suggested alerts (documented, not enforced):** `di_queue_oldest_age_seconds > 120` for 5m (stuck or under-scaled), `sum(di_worker_up) == 0` (no workers), `rate(di_jobs_total{status="failed"}[5m]) / rate(di_jobs_total[5m]) > 0.2`, `increase(di_scrape_errors_total[10m]) > 0`.

Verify it all with `bash scripts/observability_smoke.sh` (free `echo` jobs only).
