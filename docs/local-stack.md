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
