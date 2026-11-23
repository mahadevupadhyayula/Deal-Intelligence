#!/usr/bin/env bash
# WHAT:  Gate 3 live smoke test for observability (Spec 009). Starts the stack with the
#        `observability` profile and asserts: JSON log lines correlated by job_id, no raw tenant
#        id or secret in logs, internal-only metrics ports, queue-depth and oldest-age gauges
#        (the signal KEDA will scale on), job counters, each admission-rejection reason,
#        Prometheus scraping api and worker, the drain-on-stop behaviour, and Grafana provisioning.
#        Uses only free `echo` jobs: no provider spend.
# WHERE: Run from the repo root: `bash scripts/observability_smoke.sh [--keep]`. Needs Docker,
#        jq, and a `.env` with POSTGRES_PASSWORD, APP_USER_PASSWORD, LOG_HASH_KEY and
#        GRAFANA_ADMIN_PASSWORD. Exits non-zero if any check fails.
# WHY:   Unit tests prove the pieces; only a running stack proves the ports, scrape wiring and
#        real Redis queue semantics. Secrets are read from `.env` into shell variables and are
#        never printed (Grafana credentials go to curl on stdin, not the command line).
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export ECHO_MAX_DELAY_SECONDS=20   # lets echo jobs be slow so the queue can be observed
API="http://127.0.0.1:${API_HOST_PORT:-8000}"
PROM="http://127.0.0.1:${PROMETHEUS_HOST_PORT:-9090}"
GRAF="http://127.0.0.1:${GRAFANA_HOST_PORT:-3000}"
PASS=0; FAIL=0; KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
fatal(){ echo "FATAL $1"; exit 2; }
dc()   { docker compose --profile observability "$@"; }
psql_admin() { dc exec -T postgres psql -U postgres -d deal_intel -tAc "$1"; }
wait_for() { local t="$1"; shift; local i=0; while [ $i -lt "$t" ]; do "$@" >/dev/null 2>&1 && return 0; sleep 1; i=$((i+1)); done; return 1; }
submit() {  # submit KEY JSON -> "HTTP_CODE JOB_ID"
  local out; out=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/jobs" -H "Authorization: Bearer $1" \
    -H "Content-Type: application/json" -d "$2")
  echo "$(echo "$out" | tail -1) $(echo "$out" | head -1 | jq -r '.job_id // ""')"
}
job_status() { curl -s -H "Authorization: Bearer $1" "$API/v1/jobs/$2" | jq -r '.status // "none"'; }
# Metrics ports are internal-only, so read them from inside the containers.
scrape() {  # scrape SERVICE PORT
  dc exec -T "$1" python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:$2/metrics',timeout=5).read().decode())"
}
mval() {  # mval SERVICE PORT 'metric_with_labels' -> number (0 if absent)
  scrape "$1" "$2" 2>/dev/null | awk -v m="$3" '$1==m {print $2}' | head -1 | awk '{print ($1==""?0:$1)+0}'
}
mint() {
  dc run --rm -T tools python -m agents.admin create-tenant "$1" >/dev/null 2>&1
  dc run --rm -T tools python -m agents.admin create-api-key "$1" 2>/dev/null | tail -1
}
cleanup_tenants() {
  psql_admin "DELETE FROM jobs WHERE tenant_id IN ('obs-a','obs-b'); DELETE FROM api_keys WHERE tenant_id IN ('obs-a','obs-b');
              DELETE FROM tenant_usage WHERE tenant_id IN ('obs-a','obs-b'); DELETE FROM tenants WHERE tenant_id IN ('obs-a','obs-b');" >/dev/null 2>&1 || true
}

echo "== 1. build, start (with observability profile)"
dc --profile tools build >/dev/null 2>&1 || fatal "build failed"
dc up -d --wait >/dev/null 2>&1 || { dc ps -a; dc logs --tail 30; fatal "stack did not become healthy"; }
ok "stack up with prometheus and grafana"
cleanup_tenants
KEY=$(mint obs-a); [ -n "$KEY" ] || fatal "could not mint key"
PUBLISHED=$(docker ps --filter "name=deal-intelligence" --format '{{.Ports}}' | grep -cE ":910[01]->" || true)
[ "$PUBLISHED" = "0" ] && ok "metrics ports 9100/9101 are not published to the host" || bad "a metrics port is published"
curl -s -m 2 "http://127.0.0.1:9101/metrics" >/dev/null 2>&1 && bad "api metrics reachable from the host" || ok "api metrics not reachable from the host"

echo "== 2. structured logs and correlation"
read -r CODE JOB <<<"$(submit "$KEY" '{"kind":"echo","payload":{"note":"sk-abcdefghijklmnop1234"}}')"
[ "$CODE" = "202" ] || bad "submit -> $CODE"
wait_for 40 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY' $API/v1/jobs/$JOB | jq -r .status)\" = succeeded ]" && ok "echo job succeeded" || bad "job did not succeed"
sleep 1
WLOG=$(dc logs --no-log-prefix worker 2>/dev/null)
echo "$WLOG" | jq -R 'fromjson? | select(.job_id=="'"$JOB"'" and .msg=="job.started")' | grep -q job_id && ok "worker logged job.started as JSON with the job_id" || bad "no job.started line"
echo "$WLOG" | jq -R 'fromjson? | select(.job_id=="'"$JOB"'" and .msg=="job.finished" and .status=="succeeded" and (.duration_ms|type=="number"))' | grep -q job_id && ok "worker logged job.finished (status, duration_ms)" || bad "no job.finished line"
ALOG=$(dc logs --no-log-prefix api 2>/dev/null)
echo "$ALOG" | jq -R 'fromjson? | select(.msg=="http.request" and .route=="/v1/jobs" and .method=="POST")' | grep -q route && ok "api logged a structured access line with the route template" || bad "no api access line"
ALL=$(dc logs 2>&1)
echo "$ALL" | grep -q "obs-a" && bad "raw tenant id appears in logs" || ok "raw tenant id never appears in logs (hashed only)"
echo "$ALL" | grep -q "sk-abcdefghijklmnop1234" && bad "payload secret leaked into logs" || ok "payload content/secret never logged"
echo "$ALL" | grep -qF "$KEY" && bad "API key appears in logs" || ok "API key never appears in logs"
TH_API=$(echo "$ALOG" | jq -rR 'fromjson? | select(.tenant!=null) | .tenant' | head -1)
TH_WRK=$(echo "$WLOG" | jq -rR 'fromjson? | select(.tenant!=null) | .tenant' | head -1)
[ -n "$TH_API" ] && [ "$TH_API" = "$TH_WRK" ] && ok "api and worker hash the tenant identically (shared LOG_HASH_KEY)" || bad "tenant hash differs ($TH_API vs $TH_WRK)"

echo "== 3. queue depth and oldest age (what KEDA will read in Spec 010)"
dc stop worker >/dev/null 2>&1
IDS=()
for i in 1 2 3 4 5; do read -r C I <<<"$(submit "$KEY" "{\"kind\":\"echo\",\"payload\":{\"delay_seconds\":3,\"n\":$i}}")"; IDS+=("$I"); [ "$C" = 202 ] || bad "submit $i -> $C"; done
D=$(mval api 9101 di_queue_depth); A1=$(mval api 9101 di_queue_oldest_age_seconds)
[ "$D" = "5" ] && ok "di_queue_depth == 5 with the worker stopped" || bad "queue depth is $D"
sleep 3
A2=$(mval api 9101 di_queue_oldest_age_seconds)
awk -v a="$A1" -v b="$A2" 'BEGIN{exit !(b>a)}' && ok "di_queue_oldest_age_seconds grows while jobs wait ($A1 -> $A2)" || bad "oldest age did not grow ($A1 -> $A2)"
dc start worker >/dev/null 2>&1
wait_for 60 bash -c "[ \"\$(docker compose exec -T api python -c \"import urllib.request as u;print(u.urlopen('http://127.0.0.1:9101/metrics').read().decode())\" | awk '\$1==\"di_queue_depth\"{print \$2}')\" = 0.0 ]" && ok "depth returns to 0 after the worker drains the queue" || bad "queue did not drain"
for id in "${IDS[@]}"; do wait_for 30 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY' $API/v1/jobs/$id | jq -r .status)\" = succeeded ]" || bad "job $id not succeeded"; done
S=$(mval worker 9100 'di_jobs_total{kind="echo",status="succeeded"}')
awk -v v="$S" 'BEGIN{exit !(v==5)}'  && ok "worker counter di_jobs_total{echo,succeeded} == 5" || bad "worker succeeded counter is $S"
H=$(mval worker 9100 'di_job_duration_seconds_count{kind="echo"}')
awk -v v="$H" 'BEGIN{exit !(v==5)}'  && ok "job duration histogram observed 5 jobs" || bad "duration count is $H"
[ "$(mval worker 9100 di_worker_up)" = "1" ] && ok "di_worker_up == 1" || bad "worker_up"

echo "== 4. admission rejections, one of each reason"
# A fresh tenant: rate-limit counters are per tenant per minute and step 3 already used obs-a's.
KEY2=$(mint obs-b); [ -n "$KEY2" ] || fatal "could not mint second key"
dc stop worker >/dev/null 2>&1
RATE_LIMIT_JOBS_PER_MINUTE=5 TENANT_MAX_ACTIVE_JOBS=2 TENANT_DAILY_MAX_LLM_CALLS=0 \
  dc up -d --no-deps --force-recreate --wait api >/dev/null 2>&1 || fatal "api recreate failed"
read -r C1 _ <<<"$(submit "$KEY2" '{"kind":"lead_research","payload":{}}')"      # quota
submit "$KEY2" '{"kind":"echo"}' >/dev/null; submit "$KEY2" '{"kind":"echo"}' >/dev/null
read -r C4 _ <<<"$(submit "$KEY2" '{"kind":"echo"}')"                              # active cap
C5=$(python3 -c "import json;print(json.dumps({'kind':'echo','payload':{'x':'a'*70000}}))" | curl -s -o /dev/null -w "%{http_code}" -X POST "$API/v1/jobs" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @-)
submit "$KEY2" '{"kind":"echo"}' >/dev/null    # 5th request through the limiter: still allowed (cap refuses it)
read -r C6 _ <<<"$(submit "$KEY2" '{"kind":"echo"}')"                              # 6th: over RATE=5
echo "INFO  statuses: quota=$C1 cap=$C4 payload=$C5 rate=$C6"
for r in daily_quota active_cap payload rate; do
  V=$(mval api 9101 "di_admission_rejections_total{reason=\"$r\"}")
  awk -v v="$V" 'BEGIN{exit !(v>=1)}' && ok "di_admission_rejections_total{reason=$r} >= 1" || bad "rejection reason $r not counted ($V)"
done
dc up -d --no-deps --force-recreate --wait api >/dev/null 2>&1   # back to default limits
dc start worker >/dev/null 2>&1

echo "== 5. Prometheus scrapes api and worker"
wait_for 40 bash -c "[ \"\$(curl -s '$PROM/api/v1/query?query=sum(up)' | jq -r '.data.result[0].value[1]')\" = 2 ]" && ok "Prometheus: api and worker targets are up" || bad "prometheus targets not both up"
[ "$(curl -s "$PROM/api/v1/query?query=sum(di_queue_depth)" | jq -r '.data.result | length')" = "1" ] && ok "Prometheus answers sum(di_queue_depth)" || bad "no queue depth in prometheus"

echo "== 6. a worker stopped mid-job drains (Spec 010) and the outcome is observable"
read -r C SLOW <<<"$(submit "$KEY" '{"kind":"echo","payload":{"delay_seconds":20}}')"
wait_for 30 bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $KEY' $API/v1/jobs/$SLOW | jq -r .status)\" = running ]" || bad "slow job never started"
dc stop worker >/dev/null 2>&1
[ "$(job_status "$KEY" "$SLOW")" = "succeeded" ] && ok "SIGTERM drained: the running job finished and succeeded" || bad "job ended $(job_status "$KEY" "$SLOW")"
dc logs --no-log-prefix worker 2>/dev/null | jq -R 'fromjson? | select(.job_id=="'"$SLOW"'" and .msg=="job.finished" and .status=="succeeded")' | grep -q succeeded \
  && ok "job.finished status=succeeded was logged during the drain" || bad "no job.finished line for the drained job"
dc start worker >/dev/null 2>&1

echo "== 7. Grafana provisioning"
GCFG="user = \"admin:$GRAFANA_ADMIN_PASSWORD\""
wait_for 40 bash -c "curl -sf $GRAF/api/health" || bad "grafana not healthy"
curl -s -K - "$GRAF/api/datasources" <<<"$GCFG" | jq -e '.[] | select(.type=="prometheus")' >/dev/null && ok "Grafana datasource provisioned" || bad "no datasource"
curl -s -K - "$GRAF/api/search?query=Deal" <<<"$GCFG" | jq -e '.[] | select(.uid=="deal-intel-overview")' >/dev/null && ok "Grafana dashboard provisioned" || bad "no dashboard"
[ "$(curl -s -o /dev/null -w '%{http_code}' $GRAF/api/datasources)" = "401" ] && ok "Grafana rejects anonymous access" || bad "grafana anonymous access"

echo "== 8. final log hygiene"
ALL=$(dc logs 2>&1)
echo "$ALL" | grep -q "obs-a" && bad "raw tenant id in logs (final)" || ok "no raw tenant id in any container log"
LEAKS=$(echo "$ALL" | python3 -c "
import os,sys
log=sys.stdin.read()
vals=[os.environ.get(k,'') for k in ('OPENAI_API_KEY','TAVILY_API_KEY','LANGSMITH_API_KEY','POSTGRES_PASSWORD','APP_USER_PASSWORD','LOG_HASH_KEY','GRAFANA_ADMIN_PASSWORD')]
print(sum(1 for v in vals if len(v)>=8 and v in log))")
[ "$LEAKS" = "0" ] && ok "no secret value in logs" || bad "$LEAKS secret value(s) in logs"

cleanup_tenants
[ $KEEP -eq 1 ] || dc down >/dev/null 2>&1
echo "== result: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
