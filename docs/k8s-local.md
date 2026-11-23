# Local Kubernetes runbook (kind + KEDA, Spec 010)

The same image and environment contract as Compose (`docs/local-stack.md`), on a one-node kind cluster, with workers that scale from zero on the real queue backlog. Nothing here costs money or touches a cloud account.

## First run
Prerequisites: Docker, `kind`, `kubectl`, and a `.env` with `POSTGRES_PASSWORD`, `APP_USER_PASSWORD` and `LOG_HASH_KEY` (provider keys are optional).
```bash
bash scripts/k8s_deploy.sh
```
It creates the cluster, builds and loads the image (no registry), installs KEDA from a pinned, checksum-verified release manifest, creates the Secrets from `.env` (values go to `kubectl` on stdin and are never printed), starts Postgres and Redis, runs the migration Job, then applies api, worker and the scaler. It is safe to re-run.

## Day to day
```bash
kubectl --context kind-deal-intel -n deal-intel get pods,scaledobject
kubectl --context kind-deal-intel -n deal-intel port-forward svc/api 8000:8000      # the API on localhost:8000
bash scripts/k8s_admin.sh create-tenant acme                                        # one-off admin Job (tools image)
bash scripts/k8s_admin.sh create-api-key acme
kubectl --context kind-deal-intel -n deal-intel logs -l app.kubernetes.io/name=worker --prefix
bash scripts/k8s_smoke.sh            # Gate 3 (add --keep to leave the cluster up)
bash scripts/k8s_down.sh             # delete the cluster; nothing is left running
```
After editing code: `docker compose build migrate && kind load docker-image deal-intelligence:local --name deal-intel && kubectl --context kind-deal-intel -n deal-intel rollout restart deploy/api` (workers pick up the new image the next time they start).

## How scaling works
`ScaledObject/worker` (0 to 5 replicas) reads `di_queue_outstanding` (waiting + running jobs) from the API's metrics port through KEDA's `metrics-api` scaler, targeting about 2 outstanding jobs per worker. KEDA's built-in `redis` scaler cannot be used: ARQ keeps its queue in a sorted set, not a list. If the API is unreachable, KEDA's `fallback` keeps one worker rather than stalling the queue.

**Scale-down never kills a running job.** On SIGTERM a worker stops picking jobs and finishes the ones it has (`WorkerSettings.job_completion_wait` = `JOB_TIMEOUT_SECONDS`); the pod's `terminationGracePeriodSeconds` (330 s) is longer than that wait. Anything still running after the wait is cancelled and recorded `failed ... resubmit`.

**A hard kill is recovered.** If a worker dies without draining (OOMKilled, node loss, `kill -9`), its job would stay `running` and count toward the tenant's active-job cap. A reaper loop in the **API** (not the workers: with zero workers nobody would reap) calls a narrow SECURITY DEFINER SQL function once a minute and fails jobs that have been `running` longer than `REAPER_MAX_AGE_SECONDS` (default `JOB_TIMEOUT_SECONDS + 120`) with `worker lost; resubmit`. It does not rerun them. The orphaned queue entry of a killed job keeps `di_queue_outstanding` above zero until ARQ's in-progress marker expires (about the job timeout), so KEDA may keep a worker up that long.

## Environment contract (same variables as Compose)
Non-sensitive settings live in `ConfigMap/app-config`; sensitive ones in Secrets created by `scripts/k8s_deploy.sh`. A test keeps this table and the manifests in agreement.

| Variable | Source | Goes to |
|---|---|---|
| `APP_ENV` | ConfigMap | api, worker |
| `REDIS_URL` | ConfigMap | api, worker (no password locally) |
| `LOG_FORMAT` | ConfigMap | api, worker |
| `LOG_LEVEL` | ConfigMap | api, worker |
| `METRICS_BIND_HOST` | ConfigMap | api, worker |
| `API_METRICS_PORT` | ConfigMap | api |
| `WORKER_METRICS_PORT` | ConfigMap | worker |
| `RATE_LIMIT_JOBS_PER_MINUTE` | ConfigMap | api |
| `TENANT_MAX_ACTIVE_JOBS` | ConfigMap | api |
| `TENANT_DAILY_MAX_LLM_CALLS` | ConfigMap | api, worker |
| `TENANT_DAILY_MAX_SEARCHES` | ConfigMap | api, worker |
| `JOB_TIMEOUT_SECONDS` | ConfigMap | api, worker (drain wait and reaper age derive from it) |
| `PRICE_PER_LLM_CALL` | ConfigMap | worker |
| `PRICE_PER_SEARCH` | ConfigMap | worker |
| `ECHO_MAX_DELAY_SECONDS` | ConfigMap | worker (0 = disabled; the smoke test raises it) |
| `REAPER_ENABLED` | ConfigMap | api |
| `REAPER_INTERVAL_SECONDS` | ConfigMap | api |
| `DATABASE_URL` | Secret `app-secrets` (`app_user`) | api, worker |
| `LOG_HASH_KEY` | Secret `app-secrets` | api, worker (must be identical) |
| `ADMIN_DATABASE_URL` | Secret `admin-secrets` | migrate Job and admin Jobs only |
| `APP_USER_PASSWORD` | Secret `admin-secrets` | migrate Job and admin Jobs only |
| `POSTGRES_PASSWORD` | Secret `postgres-secrets` | Postgres StatefulSet |
| `TAVILY_API_KEY` | Secret `provider-secrets` (optional) | worker only |
| `OPENAI_API_KEY` | Secret `provider-secrets` (optional) | worker only |
| `ANTHROPIC_API_KEY` | Secret `provider-secrets` (optional) | worker only |
| `LANGSMITH_API_KEY` | Secret `provider-secrets` (optional) | worker only |

`LLM_MODEL`, `LANGSMITH_TRACING` and `LANGSMITH_PROJECT` are also copied into `provider-secrets` when set in `.env`.

## Things worth knowing
- **Service links are disabled** (`enableServiceLinks: false`). Kubernetes would otherwise inject `WORKER_METRICS_PORT=tcp://10.96.x.x:9100` for the Service called `worker-metrics`, which collides with our own setting of that name and crashes the pod at startup.
- **Pod Security `restricted` is enforced** on the namespace: non-root, no privilege escalation, all capabilities dropped, RuntimeDefault seccomp. Our pods also use a read-only root filesystem with a `/tmp` emptyDir.
- **Kubernetes Secrets here are only base64** in the local etcd. Real secret handling (Secrets Manager, KMS, IRSA) is Spec 011.
- Migrations run as a Job *before* api and worker; the runner holds an advisory lock, so parallel runs are safe. Jobs are immutable, so the deploy script deletes the previous Job first.
- Metrics (`di_*`, Spec 009) are served on internal ports only; there is no Prometheus in the cluster (KEDA reads the gauge directly). Read them with `kubectl exec deploy/api -- python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:9101/metrics').read().decode())"`.
