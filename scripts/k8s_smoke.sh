#!/usr/bin/env bash
# WHAT:  Gate 3 live smoke test for Spec 010 on a local kind cluster: probes, KEDA scale from zero
#        and back to zero, graceful drain, hard-kill reaping, KEDA fallback, tenant isolation,
#        secret scoping, pod security, and a NetworkPolicy enforcement measurement. Free `echo`
#        jobs only (no provider spend); the golden suite runs in-cluster as equivalence evidence.
# WHERE: Repo root: `bash scripts/k8s_smoke.sh [--keep]`. Calls scripts/k8s_deploy.sh first
#        (idempotent). Needs docker, kind, kubectl, jq, curl and a `.env`.
# WHY:   Static manifest tests prove shape; only a running cluster proves that KEDA can read the
#        queue signal, that SIGTERM really drains, and that a force-killed worker is recovered.
#        Secrets are never printed. Timings are generous: this is a correctness test, not a benchmark.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
CTX=kind-deal-intel; NS=deal-intel; PORT=18000; API="http://127.0.0.1:$PORT"
PASS=0; FAIL=0; KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
fatal(){ echo "FATAL $1"; exit 2; }
k()    { kubectl --context "$CTX" -n "$NS" "$@"; }
wait_for() { local t="$1"; shift; local i=0; while [ $i -lt "$t" ]; do "$@" >/dev/null 2>&1 && return 0; sleep 1; i=$((i+1)); done; return 1; }
psql_admin() { k exec postgres-0 -- psql -U postgres -d deal_intel -tAc "$1" 2>/dev/null | tr -d ' '; }
PF_PID=""
pf_stop()  { [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null; PF_PID=""; }
pf_start() { pf_stop; k port-forward deploy/api "$PORT:8000" >/dev/null 2>&1 & PF_PID=$!; wait_for 20 curl -sf "$API/healthz"; }
trap pf_stop EXIT
status_of() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
submit() {  # submit KEY JSON -> "HTTP_CODE JOB_ID"
  local out; out=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/jobs" -H "Authorization: Bearer $1" -H "Content-Type: application/json" -d "$2")
  echo "$(echo "$out" | tail -1) $(echo "$out" | head -1 | jq -r '.job_id // ""')"
}
job_status() { curl -s -H "Authorization: Bearer $1" "$API/v1/jobs/$2" | jq -r '.status // "none"'; }
job_error()  { curl -s -H "Authorization: Bearer $1" "$API/v1/jobs/$2" | jq -r '.error // ""'; }
replicas()   { k get deploy worker -o jsonpath='{.spec.replicas}'; }
ready_workers() { k get pods -l app.kubernetes.io/name=worker --no-headers 2>/dev/null | grep -c Running || true; }
api_metric_sum() {  # sum a metric over all api pods (reaper may run on either)
  local total=0 pod v
  for pod in $(k get pods -l app.kubernetes.io/name=api -o name); do
    v=$(k exec "$pod" -- python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:9101/metrics').read().decode())" 2>/dev/null | awk -v m="$1" '$1==m {print $2}' | head -1)
    total=$(awk -v a="$total" -v b="${v:-0}" 'BEGIN{print a+b}')
  done; echo "$total"
}
admin() { bash scripts/k8s_admin.sh "$@"; }
cleanup_tenants() { psql_admin "DELETE FROM jobs WHERE tenant_id IN ('k8s-a','k8s-b'); DELETE FROM api_keys WHERE tenant_id IN ('k8s-a','k8s-b'); DELETE FROM tenant_usage WHERE tenant_id IN ('k8s-a','k8s-b'); DELETE FROM tenants WHERE tenant_id IN ('k8s-a','k8s-b');" >/dev/null; }
count_status() { psql_admin "SELECT count(*) FROM jobs WHERE tenant_id='k8s-a' AND status='$1'"; }

echo "== 1. deploy (cluster, KEDA, secrets, datastores, migrate, api)"
bash scripts/k8s_deploy.sh >/tmp/k8s_deploy.out 2>&1 || { tail -20 /tmp/k8s_deploy.out; fatal "deploy failed"; }
ok "cluster up; migrate Job complete; api rolled out"
[ "$(k get job migrate -o jsonpath='{.status.succeeded}')" = "1" ] && ok "migrate Job Complete" || bad "migrate job"
[ "$(k get deploy api -o jsonpath='{.status.readyReplicas}')" = "2" ] && ok "api 2/2 Ready" || bad "api replicas"
[ "$(replicas)" = "0" ] && ok "worker idle at 0 replicas" || bad "worker replicas not 0 ($(replicas))"
# Smoke-only config: slow echo jobs, higher caps, fast reaper with its 60s minimum age.
k patch configmap app-config --type merge -p '{"data":{"ECHO_MAX_DELAY_SECONDS":"20","TENANT_MAX_ACTIVE_JOBS":"50","RATE_LIMIT_JOBS_PER_MINUTE":"500","REAPER_INTERVAL_SECONDS":"10","REAPER_MAX_AGE_SECONDS":"60"}}' >/dev/null
k rollout restart deploy/api >/dev/null && k rollout status deploy/api --timeout=180s >/dev/null || fatal "api restart failed"
pf_start || fatal "port-forward failed"

echo "== 2. probes"
[ "$(status_of $API/healthz)" = "200" ] && ok "/healthz 200" || bad "/healthz"
[ "$(status_of $API/readyz)" = "200" ] && ok "/readyz 200" || bad "/readyz"
k scale statefulset/redis --replicas=0 >/dev/null
wait_for 60 bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $API/readyz)\" = 503 ]" && ok "redis down -> /readyz 503" || bad "readyz did not go 503"
[ "$(status_of $API/healthz)" = "200" ] && ok "redis down -> /healthz still 200" || bad "healthz depends on redis"
wait_for 60 bash -c "[ \"\$(kubectl --context $CTX -n $NS get pods -l app.kubernetes.io/name=api -o json | jq '[.items[]|select(.status.containerStatuses[0].ready==false)]|length')\" -ge 1 ]" && ok "api pods go NotReady (readiness probe works)" || bad "pods stayed Ready"
[ "$(k get pods -l app.kubernetes.io/name=api -o json | jq '[.items[].status.containerStatuses[0].restartCount]|add')" = "0" ] && ok "no api restarts during the outage (liveness independent)" || bad "api restarted"
k scale statefulset/redis --replicas=1 >/dev/null; k rollout status statefulset/redis --timeout=120s >/dev/null
wait_for 90 bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $API/readyz)\" = 200 ]" && ok "redis back -> /readyz 200" || bad "readyz did not recover"

echo "== 3. tenants and keys (one-off admin Jobs with the tools image)"
cleanup_tenants
admin create-tenant k8s-a >/dev/null; admin create-tenant k8s-b >/dev/null
KEY_A=$(admin create-api-key k8s-a | tail -1); KEY_B=$(admin create-api-key k8s-b | tail -1)
[ -n "$KEY_A" ] && [ -n "$KEY_B" ] && ok "tenants and API keys created" || fatal "could not mint keys"

echo "== 4. scale from zero (12 slow jobs), then back to zero"
IDS=(); MAXR=0
for i in $(seq 1 12); do read -r C I <<<"$(submit "$KEY_A" "{\"kind\":\"echo\",\"payload\":{\"delay_seconds\":12,\"n\":$i}}")"; IDS+=("$I"); [ "$C" = 202 ] || bad "submit $i -> $C"; done
END=$((SECONDS+240)); while [ $SECONDS -lt $END ]; do
  R=$(replicas); [ "${R:-0}" -gt "$MAXR" ] && MAXR=$R
  [ "$(count_status succeeded)" = "12" ] && break; sleep 2
done
[ "$MAXR" -ge 2 ] && [ "$MAXR" -le 5 ] && ok "KEDA scaled the worker from 0 up to $MAXR replicas (max 5)" || bad "max replicas observed: $MAXR"
[ "$(count_status succeeded)" = "12" ] && ok "all 12 jobs succeeded" || bad "succeeded=$(count_status succeeded) failed=$(count_status failed)"
[ "$(count_status failed)" = "0" ] && ok "no job failed during scale-out" || bad "failed jobs during scale-out"
wait_for 240 bash -c "[ \"\$(kubectl --context $CTX -n $NS get deploy worker -o jsonpath='{.spec.replicas}')\" = 0 ]" && ok "worker scaled back to 0 after the queue emptied" || bad "worker still at $(replicas)"

echo "== 5. graceful drain (graceful pod delete while a job is running)"
for i in 1 2 3; do read -r C I <<<"$(submit "$KEY_A" "{\"kind\":\"echo\",\"payload\":{\"delay_seconds\":20,\"d\":$i}}")"; DIDS+=("$I"); done
wait_for 120 bash -c "[ \"\$(kubectl --context $CTX -n $NS get deploy worker -o jsonpath='{.status.readyReplicas}')\" -ge 1 ]" || bad "no worker came up"
sleep 3
VICTIM=""; TARGET=""
for pod in $(k get pods -l app.kubernetes.io/name=worker -o name); do
  for id in "${DIDS[@]}"; do
    if k logs "$pod" 2>/dev/null | grep -q "\"job_id\": \"$id\".*job.started\|job.started.*\"job_id\": \"$id\""; then VICTIM=$pod; TARGET=$id; break 2; fi
  done
done
[ -n "$VICTIM" ] && ok "found a worker running a job ($VICTIM)" || bad "no worker with a running job"
T0=$SECONDS; k delete "$VICTIM" --wait=true >/dev/null 2>&1; DEL_SECS=$((SECONDS-T0))
echo "INFO  pod deletion took ${DEL_SECS}s (it waits for its running jobs)"
wait_for 90 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY_A' $API/v1/jobs/$TARGET | jq -r .status)\" = succeeded ]" && ok "job on the drained pod succeeded (not cancelled)" || bad "drained job ended $(job_status "$KEY_A" "$TARGET")"
for id in "${DIDS[@]}"; do wait_for 90 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY_A' $API/v1/jobs/$id | jq -r .status)\" = succeeded ]" || bad "drain: job $id is $(job_status "$KEY_A" "$id")"; done
[ "$(count_status failed)" = "0" ] && ok "no job failed across the pod deletion" || bad "failed jobs after drain"

echo "== 6. hard kill is recovered by the reaper"
read -r C KILLJOB <<<"$(submit "$KEY_A" '{"kind":"echo","payload":{"delay_seconds":20,"kill":true}}')"
wait_for 120 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY_A' $API/v1/jobs/$KILLJOB | jq -r .status)\" = running ]" || bad "kill job never started"
KPOD=""; for pod in $(k get pods -l app.kubernetes.io/name=worker -o name); do k logs "$pod" 2>/dev/null | grep -q "$KILLJOB" && KPOD=$pod; done
REAP0=$(api_metric_sum di_reaped_jobs_total)
[ -n "$KPOD" ] && k delete "$KPOD" --grace-period=0 --force >/dev/null 2>&1 && ok "force-killed the worker running the job (SIGKILL, no drain)" || bad "could not kill worker"
sleep 3; [ "$(job_status "$KEY_A" "$KILLJOB")" = "running" ] && ok "job is stranded in running right after the kill (the problem)" || bad "job status after kill: $(job_status "$KEY_A" "$KILLJOB")"
wait_for 150 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY_A' $API/v1/jobs/$KILLJOB | jq -r .status)\" = failed ]" && ok "reaper failed the stranded job" || bad "stranded job never reaped (status $(job_status "$KEY_A" "$KILLJOB"))"
job_error "$KEY_A" "$KILLJOB" | grep -q "worker lost" && ok "error says: worker lost; resubmit" || bad "reaper error text"
REAP1=$(api_metric_sum di_reaped_jobs_total)
awk -v a="$REAP0" -v b="$REAP1" 'BEGIN{exit !(b>a)}' && ok "di_reaped_jobs_total incremented ($REAP0 -> $REAP1)" || bad "reaped metric did not move"
read -r C _ <<<"$(submit "$KEY_A" '{"kind":"echo"}')"; [ "$C" = "202" ] && ok "tenant can submit again" || bad "submit after reap -> $C"

echo "== 7. KEDA fallback when the metric source is down"
# Finding: the killed job's entry stays in `arq:queue` until ARQ's in-progress key expires (~job
# timeout), and `di_queue_outstanding` counts it, so KEDA keeps a worker up for those minutes.
# Remove the orphan entry here so the fallback test starts from a true zero.
k exec redis-0 -- redis-cli ZREM arq:queue "$KILLJOB" >/dev/null 2>&1
wait_for 240 bash -c "[ \"\$(kubectl --context $CTX -n $NS get deploy worker -o jsonpath='{.spec.replicas}')\" = 0 ]" && ok "worker back at 0 before the fallback test" || bad "worker did not return to 0 (fallback test would be vacuous)"
pf_stop; k scale deploy/api --replicas=0 >/dev/null
wait_for 120 bash -c "[ \"\$(kubectl --context $CTX -n $NS get deploy worker -o jsonpath='{.spec.replicas}')\" -ge 1 ]" && ok "API down: KEDA fallback keeps >= 1 worker" || bad "no fallback replica"
k scale deploy/api --replicas=2 >/dev/null; k rollout status deploy/api --timeout=180s >/dev/null; pf_start || bad "port-forward after restore"

echo "== 8. isolation and secret scoping"
[ "$(status_of -H "Authorization: Bearer $KEY_B" "$API/v1/jobs/$KILLJOB")" = "404" ] && ok "tenant B gets 404 on tenant A's job" || bad "cross-tenant read"
[ "$(status_of "$API/v1/jobs/$KILLJOB")" = "401" ] && ok "no key -> 401" || bad "unauthenticated"
[ "$(k exec deploy/api -- env | grep -c -E '^(ADMIN_DATABASE_URL|OPENAI_API_KEY|TAVILY_API_KEY|ANTHROPIC_API_KEY|LANGSMITH_API_KEY|POSTGRES_PASSWORD)=')" = "0" ] && ok "api pod has no admin URL or provider keys" || bad "api pod holds restricted secrets"
WP=$(k get pods -l app.kubernetes.io/name=worker -o name | head -1)
[ -n "$WP" ] && { [ "$(k exec "$WP" -- env | grep -c -E '^(ADMIN_DATABASE_URL|POSTGRES_PASSWORD)=')" = "0" ] && ok "worker pod has no admin URL" || bad "worker holds admin URL"; } || bad "no worker pod to inspect"
LEAKS=$( { k logs -l app.kubernetes.io/name=api --tail=-1 --prefix=false 2>/dev/null; [ -n "$WP" ] && k logs "$WP" 2>/dev/null; } | python3 -c "
import os,sys
log=sys.stdin.read()
vals=[os.environ.get(k,'') for k in ('OPENAI_API_KEY','TAVILY_API_KEY','LANGSMITH_API_KEY','POSTGRES_PASSWORD','APP_USER_PASSWORD','LOG_HASH_KEY')]
print(sum(1 for v in vals if len(v)>=8 and v in log))")
[ "$LEAKS" = "0" ] && ok "no secret value in api/worker logs" || bad "$LEAKS secret value(s) in logs"
[ "$(kubectl --context $CTX auth can-i list pods -n $NS --as=system:serviceaccount:$NS:api)" = "no" ] && ok "api ServiceAccount has no Kubernetes API rights" || bad "api SA can list pods"
k exec deploy/api -- ls /var/run/secrets/kubernetes.io >/dev/null 2>&1 && bad "service account token is mounted" || ok "no service account token mounted in api pod"

echo "== 9. pod security"
[ "$(k exec deploy/api -- id -u)" != "0" ] && ok "api runs as non-root ($(k exec deploy/api -- id -u))" || bad "api runs as root"
ROOTISH=$(k get pods -o json | jq '[.items[]|.spec.containers[]|select((.securityContext.runAsUser // 1) == 0)]|length')
[ "$ROOTISH" = "0" ] && ok "no container configured to run as root" || bad "$ROOTISH root containers"
cat <<'EOT' | k apply --dry-run=server -f - >/tmp/psa.out 2>&1
apiVersion: v1
kind: Pod
metadata: {name: psa-probe, namespace: deal-intel}
spec:
  containers:
    - {name: x, image: busybox, securityContext: {privileged: true}}
EOT
grep -qi "violates PodSecurity" /tmp/psa.out && ok "namespace rejects a privileged pod (PSA restricted enforced)" || bad "privileged pod was admitted: $(head -c 200 /tmp/psa.out)"

echo "== 10. NetworkPolicy enforcement (measured, not assumed)"
probe() {  # probe NAME LABEL -> prints "connected" or "blocked"
  k delete pod "$1" --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<EOT | k apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $1, namespace: $NS, labels: {app.kubernetes.io/name: $2}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: p
      image: deal-intelligence:local
      imagePullPolicy: IfNotPresent
      command: ["python", "-c", "import socket,sys\ntry:\n s=socket.create_connection(('postgres',5432),timeout=4);print('connected')\nexcept Exception:\n print('blocked')"]
      securityContext: {allowPrivilegeEscalation: false, runAsNonRoot: true, runAsUser: 10001, capabilities: {drop: [ALL]}, seccompProfile: {type: RuntimeDefault}}
EOT
  wait_for 60 bash -c "[ \"\$(kubectl --context $CTX -n $NS get pod $1 -o jsonpath='{.status.phase}')\" = Succeeded ]"
  k logs "$1" 2>/dev/null | tail -1
}
CONTROL=$(probe np-control admin-task); STRANGER=$(probe np-stranger stranger)
k delete pod np-control np-stranger --ignore-not-found >/dev/null 2>&1
echo "INFO  allowed pod -> $CONTROL; unlabelled pod -> $STRANGER"
if [ "$CONTROL" = "connected" ] && [ "$STRANGER" = "blocked" ]; then ok "NetworkPolicy is ENFORCED by this cluster's CNI"
elif [ "$CONTROL" = "connected" ] && [ "$STRANGER" = "connected" ]; then echo "INFO  NetworkPolicy is NOT enforced by kind's default CNI: policies are statically validated only"; ok "enforcement measured and recorded (not enforced locally)"
else bad "network probe inconclusive (control=$CONTROL stranger=$STRANGER)"; fi

echo "== 11. golden suite in-cluster (equivalence evidence)"
GOLD=$(K8S_ADMIN_TIMEOUT=420 admin -- python -m agents.evals run golden 2>&1 | grep -E "^\| .*PASS|^\| .*FAIL" | awk '{p+=($0 ~ /PASS/); f+=($0 ~ /FAIL/)} END {print "PASS=" p " FAIL=" f}')
[ "$GOLD" = "PASS=22 FAIL=0" ] && ok "golden 22/22 in the cluster image" || bad "golden: $GOLD"

cleanup_tenants
k apply -k infra/k8s/overlays/local/app >/dev/null   # restore the default ConfigMap values
[ $KEEP -eq 1 ] || bash scripts/k8s_down.sh >/dev/null 2>&1
echo "== result: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
