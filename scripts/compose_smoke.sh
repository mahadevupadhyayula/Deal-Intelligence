#!/usr/bin/env bash
# WHAT:  Gate 3 live smoke test for the Docker Compose stack (Spec 008). Builds and starts the
#        stack, then asserts health/readiness, tenant isolation (RLS), migration idempotence,
#        data persistence, queue durability, scale-out under an active-job cap, graceful
#        worker stop, and secret hygiene. Uses only free `echo` jobs: no provider spend.
# WHERE: Run from the repo root: `bash scripts/compose_smoke.sh [--keep]`. Needs Docker and a
#        `.env` with POSTGRES_PASSWORD and APP_USER_PASSWORD (see .env.example). Exits non-zero
#        on the first failed assertion.
# WHY:   Static tests (tests/test_compose_static.py) prove the files are shaped correctly; only
#        a running stack proves the startup order, health model and wiring really work. Secrets
#        are read from `.env` into shell variables and are never printed.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
API="http://127.0.0.1:${API_HOST_PORT:-8000}"
PASS=0; FAIL=0
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
check(){ local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
fatal(){ echo "FATAL $1"; exit 2; }
dc()   { docker compose "$@"; }
psql_admin() { dc exec -T postgres psql -U postgres -d deal_intel -tAc "$1"; }
status_of() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
job_status() { curl -s -H "Authorization: Bearer $1" "$API/v1/jobs/$2" | jq -r '.status // "none"'; }
submit() {  # submit KEY JSON_BODY -> prints "HTTP_CODE JOB_ID"
  local out; out=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/jobs" -H "Authorization: Bearer $1" \
    -H "Content-Type: application/json" -d "$2")
  echo "$(echo "$out" | tail -1) $(echo "$out" | head -1 | jq -r '.job_id // ""')"
}
wait_for() {  # wait_for SECONDS cmd...
  local t="$1"; shift; local i=0
  while [ $i -lt "$t" ]; do "$@" >/dev/null 2>&1 && return 0; sleep 1; i=$((i+1)); done; return 1
}
wait_job() { wait_for "${3:-40}" bash -c "[ \"\$(curl -s -H 'Authorization: Bearer $1' $API/v1/jobs/$2 | jq -r .status)\" = '$4' ]" ; }
wait_all_succeeded() {  # wait_all_succeeded KEY SECONDS id...
  local key="$1" secs="$2"; shift 2
  local end=$((SECONDS+secs)); local id
  for id in "$@"; do
    while [ "$(job_status "$key" "$id")" != "succeeded" ]; do
      [ $SECONDS -ge $end ] && return 1; sleep 1
    done
  done
}
mint() {  # mint TENANT -> prints API key (captured by caller, never echoed)
  dc run --rm -T tools python -m agents.admin create-tenant "$1" >/dev/null 2>&1
  dc run --rm -T tools python -m agents.admin create-api-key "$1" 2>/dev/null | tail -1
}
cleanup_tenants() {
  psql_admin "DELETE FROM jobs WHERE tenant_id IN ('smoke-a','smoke-b');
              DELETE FROM api_keys WHERE tenant_id IN ('smoke-a','smoke-b');
              DELETE FROM tenant_usage WHERE tenant_id IN ('smoke-a','smoke-b');
              DELETE FROM tenants WHERE tenant_id IN ('smoke-a','smoke-b');" >/dev/null 2>&1 || true
}
T0=$SECONDS

echo "== 1. build and start"
dc --profile tools build >/dev/null 2>&1 || fatal "build failed"
BUILD_SECS=$((SECONDS-T0))
dc up -d --wait >/dev/null 2>&1 || { dc ps -a; dc logs --tail 30; fatal "stack did not become healthy"; }
ok "stack up and healthy ($BUILD_SECS s build)"
[ "$(dc ps -a --format json migrate | jq -r '.ExitCode // 0' | head -1)" = "0" ] && ok "migrate exited 0" || bad "migrate exit code"
RESTARTING=$(dc ps --format json | jq -r 'select(.State=="restarting") | .Service' | wc -l | tr -d ' ')
[ "$RESTARTING" = "0" ] && ok "no restarting containers" || bad "restarting containers: $RESTARTING"

echo "== 2. health and readiness"
[ "$(curl -s $API/healthz | jq -r .status)" = "ok" ] && ok "/healthz ok" || bad "/healthz"
[ "$(status_of $API/readyz)" = "200" ] && ok "/readyz 200 (postgres + redis ok)" || bad "/readyz"
dc stop redis >/dev/null 2>&1
wait_for 20 bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $API/readyz)\" = 503 ]" && ok "redis down -> /readyz 503" || bad "readyz did not go 503"
[ "$(status_of $API/healthz)" = "200" ] && ok "redis down -> /healthz still 200 (liveness independent)" || bad "healthz depends on redis"
dc start redis >/dev/null 2>&1
wait_for 40 bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $API/readyz)\" = 200 ]" && ok "redis back -> /readyz 200" || bad "readyz did not recover"

echo "== 3. tenants, keys, jobs, isolation"
cleanup_tenants
KEY_A=$(mint smoke-a); KEY_B=$(mint smoke-b)
[ -n "$KEY_A" ] && [ -n "$KEY_B" ] && ok "tenants and API keys created via tools container" || fatal "could not mint keys"
read -r CODE JOB_A <<<"$(submit "$KEY_A" '{"kind":"echo","payload":{"hello":"a"}}')"
[ "$CODE" = "202" ] && ok "tenant A job accepted (202)" || bad "submit returned $CODE"
wait_job "$KEY_A" "$JOB_A" 40 succeeded && ok "job succeeded via worker" || bad "job did not succeed"
[ "$(status_of -H "Authorization: Bearer $KEY_B" "$API/v1/jobs/$JOB_A")" = "404" ] && ok "tenant B cannot read tenant A's job (404)" || bad "cross-tenant read"
[ "$(status_of "$API/v1/jobs/$JOB_A")" = "401" ] && ok "no key -> 401" || bad "unauthenticated access"
RLS_ROWS=$(dc exec -T -e PGPASSWORD="$APP_USER_PASSWORD" postgres psql -h 127.0.0.1 -U app_user -d deal_intel -tAc "SELECT count(*) FROM jobs" | tr -d ' ')
[ "$RLS_ROWS" = "0" ] && ok "app_user without tenant context sees 0 job rows (forced RLS)" || bad "RLS leak: $RLS_ROWS rows"
FORCED=$(psql_admin "SELECT count(*) FROM pg_class WHERE relname IN ('jobs','leads','icp_segments','lead_icp_evaluations','tenant_usage') AND relrowsecurity AND relforcerowsecurity" | tr -d ' ')
[ "$FORCED" = "5" ] && ok "forced RLS on 5 tenant tables" || bad "forced RLS count $FORCED"

echo "== 4. migrations idempotent, data persists across down/up"
dc run --rm -T migrate 2>&1 | grep -q "up to date" && ok "re-running migrate: up to date" || bad "migrate not idempotent"
dc down >/dev/null 2>&1
dc up -d --wait >/dev/null 2>&1 || fatal "stack failed to restart"
[ "$(job_status "$KEY_A" "$JOB_A")" = "succeeded" ] && ok "job and API key survive down/up (named volumes)" || bad "data lost across restart"

echo "== 5. queue durability"
dc stop worker >/dev/null 2>&1
IDS=()
for i in 1 2 3; do read -r CODE ID <<<"$(submit "$KEY_A" "{\"kind\":\"echo\",\"payload\":{\"n\":$i}}")"; IDS+=("$ID"); [ "$CODE" = "202" ] || bad "submit $i -> $CODE"; done
sleep 2
[ "$(job_status "$KEY_A" "${IDS[0]}")" = "queued" ] && ok "jobs stay queued while worker is down" || bad "jobs not queued"
dc start worker >/dev/null 2>&1
wait_all_succeeded "$KEY_A" 60 "${IDS[@]}" && ok "all 3 queued jobs ran after worker restart" || bad "queued jobs lost"

echo "== 6. scale-out under an active-job cap (cap=5, 12 submissions, 3 workers)"
dc stop worker >/dev/null 2>&1
TENANT_MAX_ACTIVE_JOBS=5 dc up -d --no-deps --force-recreate --wait api >/dev/null 2>&1 || fatal "api recreate failed"
ACCEPTED=(); REFUSED=0
for i in $(seq 1 12); do
  read -r CODE ID <<<"$(submit "$KEY_A" "{\"kind\":\"echo\",\"payload\":{\"burst\":$i}}")"
  if [ "$CODE" = "202" ]; then ACCEPTED+=("$ID"); elif [ "$CODE" = "429" ]; then REFUSED=$((REFUSED+1)); fi
done
[ "${#ACCEPTED[@]}" = "5" ] && [ "$REFUSED" = "7" ] && ok "cap enforced: 5 accepted, 7 refused (429)" || bad "cap: accepted=${#ACCEPTED[@]} refused=$REFUSED"
dc up -d --scale worker=3 --wait worker >/dev/null 2>&1 || bad "scale worker=3 failed"
[ "$(dc ps --format json worker | jq -s 'length')" = "3" ] && ok "3 worker containers running" || bad "worker count"
wait_all_succeeded "$KEY_A" 60 "${ACCEPTED[@]}" && ok "all accepted jobs succeeded" || bad "jobs did not all succeed"
DUP=$(psql_admin "SELECT count(*) FROM (SELECT job_id FROM jobs WHERE tenant_id='smoke-a' GROUP BY job_id HAVING count(*)>1) d" | tr -d ' ')
SUCC=$(psql_admin "SELECT count(*) FROM jobs WHERE tenant_id='smoke-a' AND payload ? 'burst' AND status='succeeded'" | tr -d ' ')
[ "$DUP" = "0" ] && [ "$SUCC" = "5" ] && ok "each accepted job ran exactly once (5 succeeded rows, no duplicates)" || bad "exactly-once: dup=$DUP succeeded=$SUCC"
TENANT_MAX_ACTIVE_JOBS=10 dc up -d --no-deps --force-recreate --wait api >/dev/null 2>&1
dc up -d --scale worker=1 --wait worker >/dev/null 2>&1

echo "== 7. worker stop with a job in flight (SIGTERM behaviour is recorded, not assumed)"
ECHO_MAX_DELAY_SECONDS=20 dc up -d --no-deps --force-recreate --wait worker >/dev/null 2>&1
read -r CODE SLOW <<<"$(submit "$KEY_A" '{"kind":"echo","payload":{"delay_seconds":15}}')"
wait_job "$KEY_A" "$SLOW" 20 running && ok "slow job is running" || bad "slow job never started"
T1=$SECONDS; dc stop worker >/dev/null 2>&1; STOP_SECS=$((SECONDS-T1))
FINAL=$(job_status "$KEY_A" "$SLOW")
ERR=$(curl -s -H "Authorization: Bearer $KEY_A" "$API/v1/jobs/$SLOW" | jq -r '.error // ""')
echo "INFO  worker stop took ${STOP_SECS}s; in-flight job status: $FINAL; error: $ERR"
[ "$FINAL" = "failed" ] && echo "$ERR" | grep -q "worker stopped" && ok "SIGTERM cancels the in-flight job and it is recorded failed (not stranded in running)" || bad "in-flight job ended as '$FINAL' ($ERR)"
dc up -d --force-recreate --wait worker >/dev/null 2>&1

echo "== 8. secret hygiene"
IMG_ENV=$(docker image inspect deal-intelligence:local --format '{{json .Config.Env}}')
echo "$IMG_ENV" | grep -qiE "PASSWORD|API_KEY|SECRET|TOKEN|DATABASE_URL" && bad "secret-like name in image env" || ok "image env has no secrets"
docker history --no-trunc deal-intelligence:local 2>/dev/null | grep -qiE "PASSWORD=|API_KEY=|sk-" && bad "secret in image history" || ok "image history has no secrets"
[ "$(dc exec -T api env | grep -c -E '^(ADMIN_DATABASE_URL|OPENAI_API_KEY|TAVILY_API_KEY|ANTHROPIC_API_KEY|LANGSMITH_API_KEY)=')" = "0" ] && ok "api container has no admin URL or provider keys" || bad "api container holds restricted secrets"
[ "$(dc exec -T worker env | grep -c '^ADMIN_DATABASE_URL=')" = "0" ] && ok "worker container has no admin URL" || bad "worker holds admin URL"
LEAKS=$(dc logs 2>&1 | python3 -c "
import os,sys
log=sys.stdin.read()
vals=[os.environ.get(k,'') for k in ('OPENAI_API_KEY','TAVILY_API_KEY','LANGSMITH_API_KEY','ANTHROPIC_API_KEY','POSTGRES_PASSWORD','APP_USER_PASSWORD')]
print(sum(1 for v in vals if len(v)>=8 and v in log))")
[ "$LEAKS" = "0" ] && ok "no secret value appears in container logs" || bad "$LEAKS secret value(s) found in logs"

echo "== 9. golden suite inside the tools image (equivalence evidence)"
dc run --rm -T tools python -m agents.evals run golden 2>&1 | grep -E "^\| .*PASS|^\| .*FAIL" | awk '{p+=($0 ~ /PASS/); f+=($0 ~ /FAIL/)} END {print "golden PASS=" p " FAIL=" f}' | tee /tmp/compose_golden.txt
grep -q "PASS=22 FAIL=0" /tmp/compose_golden.txt && ok "22/22 golden cases pass in the packaged image" || bad "golden suite in image"

echo "== 10. sizes and timings"
docker images --format 'INFO  image {{.Repository}}:{{.Tag}} {{.Size}}' | grep deal-intelligence
echo "INFO  cold build seconds: $BUILD_SECS (cached rebuilds are faster)"

cleanup_tenants
[ $KEEP -eq 1 ] || dc down >/dev/null 2>&1
echo "== result: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
