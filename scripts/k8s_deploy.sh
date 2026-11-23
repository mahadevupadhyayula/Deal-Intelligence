#!/usr/bin/env bash
# WHAT:  Brings up the whole stack on a local kind cluster: cluster, image, KEDA, secrets,
#        Postgres/Redis, migrations, then api + worker + scaler. Safe to re-run.
# WHERE: Repo root: `bash scripts/k8s_deploy.sh`. Needs docker, kind, kubectl and a `.env` with
#        POSTGRES_PASSWORD, APP_USER_PASSWORD and LOG_HASH_KEY (provider keys are optional).
#        Used by scripts/k8s_smoke.sh; torn down with scripts/k8s_down.sh.
# WHY:   Order matters and is easy to get wrong by hand: secrets before pods, datastores ready
#        before the migration Job, the Job complete before the app starts. Secrets are built from
#        `.env` and sent to kubectl on stdin, so values never appear in process arguments, files
#        or output. KEDA is installed from a pinned, checksum-verified release manifest.
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=deal-intel; CTX="kind-$CLUSTER"; NS=deal-intel
k() { kubectl --context "$CTX" "$@"; }
step() { echo "== $*"; }
set -a; . ./.env; set +a
for v in POSTGRES_PASSWORD APP_USER_PASSWORD LOG_HASH_KEY; do
  [ -n "${!v:-}" ] || { echo "missing $v in .env"; exit 2; }
done

step "cluster"
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --config infra/k8s/kind-cluster.yaml >/dev/null
fi
k wait --for=condition=Ready node --all --timeout=120s >/dev/null

step "images (built by Compose, loaded into the cluster; no registry)"
docker compose --profile tools build >/dev/null
kind load docker-image deal-intelligence:local deal-intelligence-tools:local --name "$CLUSTER" >/dev/null

step "KEDA"
if ! k get ns keda >/dev/null 2>&1; then
  VER=$(cat infra/k8s/keda/VERSION); TMP=$(mktemp -d)
  curl -sL -o "$TMP/keda.yaml" "https://github.com/kedacore/keda/releases/download/v$VER/keda-$VER.yaml"
  (cd "$TMP" && shasum -a 256 -c "$OLDPWD/infra/k8s/keda/SHA256" >/dev/null) || { echo "KEDA checksum mismatch"; exit 3; }
  k apply --server-side -f "$TMP/keda.yaml" >/dev/null
  rm -rf "$TMP"
fi
k -n keda wait --for=condition=Available deployment --all --timeout=180s >/dev/null

step "namespace and secrets (values come from .env; never printed)"
k apply -k infra/k8s/base/namespace >/dev/null
mksecret() {  # mksecret NAME  (KEY=VALUE lines on stdin)
  k -n "$NS" create secret generic "$1" --from-env-file=/dev/stdin --dry-run=client -o yaml | k apply -f - >/dev/null
}
APP_DB="postgresql://app_user:${APP_USER_PASSWORD}@postgres:5432/deal_intel"
mksecret postgres-secrets <<EOT
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
EOT
mksecret app-secrets <<EOT
DATABASE_URL=${APP_DB}
LOG_HASH_KEY=${LOG_HASH_KEY}
EOT
mksecret admin-secrets <<EOT
ADMIN_DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD}@postgres:5432/deal_intel
DATABASE_URL=${APP_DB}
APP_USER_PASSWORD=${APP_USER_PASSWORD}
EOT
# Provider secrets: only keys that are actually set; the worker mounts them as optional.
{
  for v in LLM_MODEL TAVILY_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY LANGSMITH_API_KEY LANGSMITH_TRACING LANGSMITH_PROJECT; do
    [ -n "${!v:-}" ] && echo "$v=${!v}"
  done
  true
} | mksecret provider-secrets

step "datastores"
k apply -k infra/k8s/overlays/local/datastores >/dev/null
k -n "$NS" rollout status statefulset/postgres statefulset/redis --timeout=240s >/dev/null

step "migrations"
k -n "$NS" delete job migrate --ignore-not-found >/dev/null
k apply -k infra/k8s/overlays/local/migrate >/dev/null
k -n "$NS" wait --for=condition=complete job/migrate --timeout=180s >/dev/null \
  || { k -n "$NS" logs job/migrate --tail=30; echo "migration failed"; exit 4; }

step "application"
k apply -k infra/k8s/overlays/local/app >/dev/null
k -n "$NS" rollout status deployment/api --timeout=240s >/dev/null
echo "ready: kubectl --context $CTX -n $NS port-forward svc/api 8000:8000"
