# Spec 010 — Local Kubernetes (kind) with KEDA Queue-Depth Autoscaling

**Status:** Done; Gates 1, 2 (N/A, equivalence evidence) and 3 pass
**Phase:** Slice 1 (Lead Research) — Deploy: local · **Depends on:** 003, 006, 008, 009

## Objective
Run the same image and environment contract as Spec 008 on a local Kubernetes cluster (kind) and prove the behaviour that Compose cannot: workers scale **from zero to N and back to zero** on the real queue backlog (KEDA), a scale-down never kills a job that is running, and a worker that dies hard does not strand a job in `running`. The manifests are the rehearsal for EKS: Spec 011 (Terraform plan) will reuse the same base and swap in managed Postgres/Redis. Nothing here costs money or touches a cloud account.

## Findings from inspecting the code (they shape the decisions)
- **KEDA's `redis` scaler cannot read ARQ's queue.** It supports lists and streams; ARQ keeps its queue in a sorted set (`arq:queue`). The architecture doc's "KEDA ScaledObject on Redis queue depth" is therefore wrong as written. Spec 009 already exposes `di_queue_depth` / `di_queue_in_flight` on the API's metrics port, so KEDA's `metrics-api` scaler reads those directly (no Prometheus needed in the cluster).
- **Scale-down would currently destroy work.** On SIGTERM ARQ cancels in-flight jobs (Spec 008 finding). With KEDA scaling down, every scale-down would fail a running LLM job. ARQ already has the fix built in: `job_completion_wait` makes the worker stop picking jobs on SIGTERM and wait for running ones. We set it and make the pod's grace period longer than it.
- **A hard kill still strands `running` jobs** (OOMKilled, node loss, SIGKILL). Kubernetes makes this a routine event rather than a rare one, and stranded rows count toward a tenant's active-job cap (Spec 006). The Spec 008 notes deferred a reaper to "a later spec"; this is where it lands (see Open Question 2).
- **Tooling on this machine:** `kubectl` v1.34 is present; `kind` and `helm` are not; Docker has 11 CPUs and about 8 GB. The design must fit in that (small requests, `maxReplicaCount` 5).

## Decisions Taken
- **kind, one node, no registry.** The image built by Compose (`deal-intelligence:local`) is loaded with `kind load docker-image`; pods use `imagePullPolicy: Never`/`IfNotPresent`. Zero external services. Trade-off: kind's default CNI may not enforce NetworkPolicy; Gate 3 tests enforcement empirically and the result decides whether the policies are "enforced" or "written and statically validated only" (recorded in the notes, not hidden).
- **Plain manifests with Kustomize (built into `kubectl -k`), no Helm.** `infra/k8s/base` (workloads: api, worker, migrate Job, ScaledObject, PDBs, ServiceAccounts, NetworkPolicies) and `infra/k8s/overlays/local` (in-cluster Postgres + Redis StatefulSets, kind-specific settings, local image). Spec 011's AWS overlay will replace the datastores with RDS/ElastiCache endpoints and the image with ECR, leaving the base untouched. Helm adds a second templating language we do not need yet.
- **KEDA installed from its pinned release manifest** (`kubectl apply --server-side -f keda-<version>.yaml`, version and checksum recorded), not Helm. It is a cluster add-on, kept out of `base`; the repo stores the pinned version and the install command, and the download needs your go-ahead (Open Question 1).
- **Scaling signal:** `metrics-api` scaler on the API's internal metrics Service (`api-metrics:9101`), reading `di_queue_depth + di_queue_in_flight` as total outstanding work (the sum is exposed as a new gauge `di_queue_outstanding`, so KEDA reads one number). Target: 2 outstanding jobs per worker replica (`targetValue: "2"`), `minReplicaCount: 0`, `maxReplicaCount: 5`, `pollingInterval: 5`, `cooldownPeriod: 60`. KEDA `fallback` (failureThreshold 3, replicas 1) keeps one worker if the metric source is unreachable, so an API restart cannot silently stall the queue.
- **Scale-down is safe by construction.** Worker `WorkerSettings.job_completion_wait = JOB_TIMEOUT_SECONDS` (default 300) so SIGTERM drains; pod `terminationGracePeriodSeconds` = job timeout + 30 s; the readiness/health probes ignore the draining state. ARQ will still cancel anything left after the wait, and the existing Spec 008 handler records it `failed ... resubmit`. The Compose `stop_grace_period` is aligned to the same value so both runtimes behave alike.
- **Stale-job reaper** (if approved): a SECURITY DEFINER SQL function `reap_stale_jobs(max_age interval)` owned by a dedicated narrow role with `BYPASSRLS`, EXECUTE granted only to `app_user`; it does one thing, `UPDATE jobs SET status='failed', error='worker lost; resubmit' WHERE status='running' AND updated_at < now() - max_age`, and returns the affected `tenant_id`/`job_id` pairs. The worker runs it from an ARQ `cron` job once a minute, guarded by a Postgres advisory lock so only one replica does the work (`max_age` = job timeout + 120 s, so a live job can never be reaped). Why a function, not a cross-tenant query from app code: forced RLS stays intact for every other statement, and the one privileged operation is a single reviewable SQL object (an RLS-bypassing `UPDATE` in Python would put a tenant-wide capability in the app role). Tenants appear in logs only as hashes; a metric `di_reaped_jobs_total` and a `job.reaped` log line make it visible. Reaping does not retry the job (retry safety is still undefined, see Spec 006 notes).
- **Least privilege in the cluster, mirroring Compose:** three Secrets, created at deploy time from `.env` by a script (never committed, values never printed): `app-secrets` (database URL for `app_user`, Redis URL, `LOG_HASH_KEY`) for api and worker; `provider-secrets` (LLM/Tavily/LangSmith keys) mounted **only** into worker; `admin-secrets` (superuser URL, `APP_USER_PASSWORD`) used **only** by the migrate Job and operator Jobs. Each workload gets its own ServiceAccount with `automountServiceAccountToken: false`.
- **Pod hardening equal to Compose, in Kubernetes terms:** namespace labelled for Pod Security Admission `restricted` (enforce), `runAsNonRoot` with uid/gid 10001, `readOnlyRootFilesystem` with an `emptyDir` on `/tmp`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`. Resource requests and limits on everything (worker: 250m/256Mi request, 1 CPU/1Gi limit).
- **Probes mirror the health model from Spec 008:** api `startupProbe` + `livenessProbe` on `/healthz`, `readinessProbe` on `/readyz`; worker liveness via `arq --check` (needs no port). PodDisruptionBudget for api (`minAvailable: 1`, two replicas). Worker has none: drain, not budget, protects jobs.
- **Migrations as a Job, run before the rollout.** `scripts/k8s_deploy.sh` applies datastores, waits ready, runs the migrate Job to completion, then applies api/worker. (A Job beats an init container: with N replicas an init container would run N migrations; ours are lock-safe but wasteful, and a failed migration should block the deploy, not crash-loop pods.)
- **Access:** `kubectl port-forward` only (no Ingress controller, no LoadBalancer). Cloud ingress/ALB belongs to Spec 011.
- **Metrics inside the cluster:** `api-metrics` and `worker-metrics` are ClusterIP Services reachable only from KEDA's namespace and (later) a scraper, via NetworkPolicy; no Prometheus/Grafana in kind in this spec (they stay a Compose profile; ServiceMonitors need an operator and add little here). KEDA reads the gauge directly; Gate 3 reads the rest with `kubectl exec`/port-forward.

## Technical Requirements

### 1. Files
- `infra/k8s/base/` — `namespace.yaml` (PSA labels), `serviceaccounts.yaml`, `api.yaml` (Deployment ×2, Service `api` 8000, Service `api-metrics` 9101, PDB), `worker.yaml` (Deployment, Service `worker-metrics`), `migrate-job.yaml`, `scaledobject.yaml`, `networkpolicies.yaml` (default deny; allow api<-clients/port-forward, api/worker -> postgres/redis, KEDA -> `api-metrics`, DNS egress; worker egress to the internet for provider APIs only), `kustomization.yaml`.
- `infra/k8s/overlays/local/` — `postgres.yaml` (StatefulSet `pgvector/pgvector:pg16`, PVC, `pg_isready` probe), `redis.yaml` (StatefulSet, AOF + `noeviction` as in Compose, PVC), `kustomization.yaml` (image `deal-intelligence:local`, config patches, `imagePullPolicy`).
- `infra/k8s/keda/README.md` — pinned KEDA version, checksum, the install command, and why it is not in `base`.
- `infra/k8s/kind-cluster.yaml` — cluster config (one control-plane node, pinned node image).
- `scripts/k8s_deploy.sh` (create cluster if absent, build/load image, install KEDA if absent, create the three Secrets from `.env`, apply, migrate, wait), `scripts/k8s_down.sh` (delete the cluster), `scripts/k8s_smoke.sh` (Gate 3).
- `workers/settings.py` — `job_completion_wait`, cron registration of the reaper; `agents/config.py` + `.env.example` — `REAPER_ENABLED`, `REAPER_INTERVAL_SECONDS`, `REAPER_MAX_AGE_SECONDS` (default derived from job timeout).
- `agents/observability/metrics.py` / `queue.py` — new gauge `di_queue_outstanding`; counter `di_reaped_jobs_total`.
- `migrations/NNN_reaper.sql` — the narrow role and the SECURITY DEFINER function (idempotent; password-less role with `NOLOGIN`; function owned by that role; `REVOKE ALL ... FROM PUBLIC`; `GRANT EXECUTE ... TO app_user`; fixed `search_path`).
- `agents/repos/job_reaper.py` + `workers/reaper.py` — thin wrapper calling the function and logging/recording results.
- `docker-compose.yml` — worker `stop_grace_period` aligned with the drain.
- `docs/k8s-local.md` — runbook; `docs/ARCHITECTURE.md` — correct the KEDA statement and the deployment path.

### 2. Environment contract
Identical variables to Compose (same `pydantic-settings` fields). Only the source changes: Compose interpolation becomes Secrets (sensitive) and a ConfigMap (non-sensitive: log level/format, limits, ports, `JOB_TIMEOUT_SECONDS`, price settings). `docs/k8s-local.md` carries a table mapping each variable to Secret/ConfigMap and the workload that receives it; a test asserts the table and the manifests agree.

### 3. Constraints from CLAUDE.md
- **Never block FastAPI / stateless workers:** unchanged; the reaper and drain are worker-side. Any worker can run the reaper; the advisory lock prevents duplicates. No worker-local state is added.
- **`tenant_id` everywhere:** the reaper is the single deliberate cross-tenant operation. It touches only `jobs.status/error` on stale `running` rows, returns ids for hashed logging, cannot read payloads/results, and has its own isolation test (a stale job of tenant A is reaped without ever exposing tenant B data, and `app_user` still cannot `SELECT` across tenants).
- **Zero hardcoded secrets:** manifests contain no secret values; Secrets are generated at deploy time; a static test scans every rendered manifest.
- **Commenting rule:** every manifest starts with a WHAT/WHERE/WHY comment block; every script and Python file has the docstring.

## Out of Scope
- Terraform, EKS, RDS/ElastiCache, IRSA, ECR (Spec 011); real ingress/TLS/ALB.
- HPA and metrics-server for the API (a fixed 2 replicas is enough locally; revisit with load testing); cluster autoscaling.
- Prometheus Operator/ServiceMonitor, Grafana in-cluster, log shipping.
- The Streamlit service (Spec 012) and per-agent deploy deltas for later slices.
- Job retry/idempotency semantics (the reaper fails stale jobs, it does not rerun them).
- Secret management beyond Kubernetes Secrets (Secrets Manager/IRSA are Spec 011; kind Secrets are only base64 in the local etcd).
- Multi-node, anti-affinity, PodTopologySpread (meaningless on one node).

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] Reaper (real test DB, as `app_user`): a `running` job older than `max_age` becomes `failed` with the "worker lost" message and its tenant/job ids are returned; a fresh `running`, a `queued` and a finished job are untouched; tenant A's and B's stale jobs are both reaped but each tenant's other data is never returned; `app_user` still sees 0 rows across tenants without a tenant context (forced RLS intact); the function is not executable by `PUBLIC`; `search_path` is fixed.
- [ ] Reaper concurrency: two simultaneous runs reap each job exactly once (advisory lock); a job that is genuinely still running (updated within `max_age`) is never reaped.
- [ ] Reaper wiring: cron registered only when `REAPER_ENABLED`; metric `di_reaped_jobs_total` and a `job.reaped` log with hashed tenant; instrumentation failure does not stop the cron.
- [ ] Drain config: `WorkerSettings.job_completion_wait` equals the job timeout; pod grace period in the manifest and Compose `stop_grace_period` are both >= it (static test).
- [ ] `di_queue_outstanding == depth + in_flight` (fake Redis, as in Spec 009 tests).
- [ ] Static manifest tests over `kubectl kustomize` output (skipped if `kubectl` missing): every container has resource requests/limits, `runAsNonRoot`, read-only root fs, dropped capabilities, `RuntimeDefault` seccomp, no `latest` tag; ServiceAccounts do not automount tokens; `provider-secrets` referenced only by the worker, `admin-secrets` only by the migrate Job; no literal secret values or `sk-`/`tvly-` shapes; PSA `restricted` label present; every Service/NetworkPolicy selector matches a workload label; ScaledObject has `minReplicaCount: 0`, a `fallback`, and targets the worker Deployment; probes present on api; variable table in `docs/k8s-local.md` matches the manifests.
- [ ] `tenant_id` isolation tests unchanged and green.

### Gate 2 — Agent Behavior
**N/A with reason:** no agent logic, prompts, tools or graph topology change. Equivalence evidence: the golden suite (22/22) still passes, and it is re-run inside the image that is loaded into kind.

### Gate 3 — Live Smoke (`scripts/k8s_smoke.sh`; free `echo` jobs with `ECHO_MAX_DELAY_SECONDS` raised; no provider spend)
- [ ] `scripts/k8s_deploy.sh` -> cluster up, KEDA ready, Postgres/Redis ready, migrate Job `Complete`, api 2/2 Ready, worker at 0 replicas (idle).
- [ ] Probes: `/healthz` and `/readyz` 200 through port-forward; stopping Redis makes pods NotReady for the api while liveness holds.
- [ ] **Scale from zero:** submit 12 slow echo jobs -> KEDA scales the worker Deployment from 0 up (observe replicas > 1, <= 5); all 12 jobs `succeeded`, each exactly once.
- [ ] **Scale back to zero** after the queue empties and the cooldown passes.
- [ ] **Drain:** while 4 slow jobs are running on several workers, force a scale-down (`kubectl scale`/delete the KEDA trigger) -> no job ends `failed`; pods exit only after their jobs finish (`terminationGracePeriodSeconds` respected); the `job.finished` logs show `succeeded`.
- [ ] **Hard kill:** `kubectl delete pod --grace-period=0 --force` on a worker running a job -> within `REAPER_MAX_AGE` (smoke uses a short value) the job becomes `failed: worker lost; resubmit`, the tenant's active-job count drops, and `di_reaped_jobs_total` increments; the tenant can submit again.
- [ ] **Fallback:** scale the API to 0 -> the worker keeps >= 1 replica (KEDA fallback), no job is lost; restored after.
- [ ] **Isolation and secrets:** tenant B gets 404 on tenant A's job; the api pod environment has no admin URL or provider keys; worker has no admin URL; `kubectl get secret` values never printed; no secret value in pod logs.
- [ ] **Pod security:** `kubectl auth can-i` from each pod's ServiceAccount is denied; no pod runs as root; the namespace rejects a deliberately privileged test pod (PSA `restricted` enforced).
- [ ] **NetworkPolicy enforcement test:** a throwaway pod without the allow label cannot reach Postgres or Redis. Result (enforced / not enforced by kind's CNI) is written to the notes; if not enforced, the policies are marked "validated statically only" and the cloud overlay (Spec 011) is flagged to verify it with a policy-capable CNI.
- [ ] `scripts/k8s_down.sh` removes the cluster; nothing is left running; the Compose stack still passes its smoke afterwards (31/31).

### Definition of Done
- [ ] All gates pass; Verification Report (Pass/Fail) output before the build log
- [ ] Every file has WHAT/WHERE/WHY docstring/comment block
- [ ] Build log written; README progress table updated (010 -> Done)
- [ ] `docs/k8s-local.md` written; `docs/ARCHITECTURE.md` corrected (KEDA scaler type, deployment path)

## Open Questions (need your decision before approval)
1. **Install tooling?** `kind` is not installed (`brew install kind`, about 10 MB) and KEDA is a pinned release manifest downloaded from GitHub (about 3 MB, applied into the cluster). Both are downloads, so I will only do them with your go-ahead. Recommended: yes to both; no Helm. (kind also pulls the node image, about 1 GB, from a public registry.)
2. **Include the stale-job reaper in this spec?** Recommended: yes. Pod kills are routine on Kubernetes, and stranded `running` rows lock a tenant out via the active-job cap, so the scaling story is incomplete without it. Cost: one migration (a narrow `BYPASSRLS` role and a SECURITY DEFINER function) that you should review. Alternative: ship drain only and leave the reaper as a documented gap until the cloud spec.
3. **Scaling signal:** total outstanding jobs (waiting + running) at 2 per worker (recommended: running jobs also occupy a worker slot, so scaling on waiting alone would flap) versus waiting-only. Target 2, max 5 workers are placeholders you can change.
4. **API HPA / metrics-server now?** Recommended: no. Fixed 2 replicas, and revisit with load numbers when the UI exists.

## Resolved Questions
All four recommendations were approved: (1) install `kind` (v0.33.0 via Homebrew) and fetch the pinned KEDA 2.21.1 release manifest (SHA-256 recorded; no Helm); (2) reaper included; (3) scale on waiting + running at 2 per worker, max 5; (4) no API HPA or metrics-server.

## Implementation Notes (deviations and findings)
- **The reaper runs in the API, not as a worker cron (deviation).** The draft put it in a worker cron job, but the worker Deployment scales to zero: a tenant sitting at its active-job cap with stranded rows could not enqueue anything, so no worker would ever start to reap them, a permanent lockout. `api/reaper.py` is a small async loop started in the API lifespan (one cheap query a minute, off the request path). Two API replicas are safe: the SQL function reaps each row exactly once.
- **No advisory lock needed.** The UPDATE row-locks each job and the loser of a race re-checks `status = 'running'`; a 4-way concurrent test over 12 stale jobs reaped each exactly once. The function also refuses `max_age < 60s` so a typo cannot fail every live job.
- **Service links broke the first deploy.** Kubernetes injects `WORKER_METRICS_PORT=tcp://10.96.x.x:9100` for the Service `worker-metrics`, which collides with our own setting of that name; pydantic refused to start. Every pod now sets `enableServiceLinks: false` and a static test enforces it.
- **A real logging bug surfaced in the cluster logs:** `configure_logging` re-enabled propagation on `uvicorn.access`, undoing `--no-access-log`, so probe lines polluted the logs (also in Compose since Spec 009). `uvicorn.access` is no longer touched.
- **NetworkPolicy IS enforced by kind v0.33's default CNI** (measured: a pod labelled as an allowed client connected to Postgres, an unlabelled pod was blocked). So the policies are live, not just written. This also exposed a gap in my own design: with default-deny egress the migrate Job and admin Jobs need an explicit egress rule to Postgres (added).
- **Drain was one setting.** ARQ already implements it: `job_completion_wait`. Measured: deleting a worker pod that was running a 20 s job took 19 s and the job succeeded. Compose `stop_grace_period` is now 330 s (default via `WORKER_STOP_GRACE`) and Spec 008's smoke step 7 now asserts the job *succeeds* on `docker compose stop worker` (it used to assert `failed`).
- **Orphaned queue entry after a hard kill.** The killed job's entry stays in `arq:queue` until ARQ's in-progress key expires (about the job timeout), and `di_queue_outstanding` counts it, so KEDA keeps a worker up for those minutes. Harmless but worth knowing; the smoke test removes the entry (`ZREM`) so the fallback test starts from a true zero.
- **KEDA reads the gauge fine, and HPA shows `<unknown>` until a pod exists** (KEDA owns scale-from-zero; the HPA minimum shown is 1). Observed scale: 0 -> 5 for 12 slow jobs, back to 0 after cooldown.
- **New settings and metrics:** `REAPER_ENABLED`, `REAPER_INTERVAL_SECONDS`, `REAPER_MAX_AGE_SECONDS`, `WORKER_STOP_GRACE`; gauge `di_queue_outstanding`, counter `di_reaped_jobs_total`.
- **For Spec 011:** migration 0007 creates a `BYPASSRLS` role; on RDS this needs an account allowed to do so, so the cloud overlay must run migrations with the right privileges. The Postgres/Redis StatefulSets are local-only and not in `base`.
- **Smoke harness bug (not a product bug):** the first full run failed only the in-cluster golden step because the admin Job wait was 120 s while the suite takes about 50 s plus startup under a CPU limit; the wait is now configurable (`K8S_ADMIN_TIMEOUT`) and the limit raised.
