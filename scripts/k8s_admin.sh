#!/usr/bin/env bash
# WHAT:  Runs one operator command (`python -m agents.admin ...` or any command) in the cluster
#        as a throwaway Job using the `tools` image and the admin secrets, then prints its output.
# WHERE: Repo root: `bash scripts/k8s_admin.sh create-tenant acme` or
#        `bash scripts/k8s_admin.sh -- python -m agents.evals run golden`.
# WHY:   Creating tenants and API keys needs the superuser URL, which only `admin-secrets` holds
#        and only this short-lived, labelled pod may read (network policy allows `admin-task`
#        pods to reach Postgres). The pod obeys the namespace's `restricted` policy.
set -euo pipefail
CTX="kind-deal-intel"; NS=deal-intel
if [ "${1:-}" = "--" ]; then shift; ARGS=("$@"); else ARGS=(python -m agents.admin "$@"); fi
NAME="admin-$(date +%s)-$RANDOM"
JSON_ARGS=$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1:]))' "${ARGS[@]}")
kubectl --context "$CTX" -n "$NS" apply -f - >/dev/null <<EOT
apiVersion: batch/v1
kind: Job
metadata: {name: $NAME, namespace: $NS}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 300
  template:
    metadata:
      labels: {app.kubernetes.io/name: admin-task}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      enableServiceLinks: false
      securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: task
          image: deal-intelligence-tools:local
          imagePullPolicy: IfNotPresent
          command: $JSON_ARGS
          envFrom:
            - secretRef: {name: admin-secrets}
            - secretRef: {name: provider-secrets, optional: true}
          env:
            - {name: EVAL_REPORT_DIR, value: /tmp/eval-reports}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 768Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            capabilities: {drop: ["ALL"]}
            seccompProfile: {type: RuntimeDefault}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: tmp, emptyDir: {}}
EOT
kubectl --context "$CTX" -n "$NS" wait --for=condition=complete "job/$NAME" --timeout="${K8S_ADMIN_TIMEOUT:-120}s" >/dev/null 2>&1 \
  || { kubectl --context "$CTX" -n "$NS" logs "job/$NAME" --tail=20 >&2; exit 1; }
kubectl --context "$CTX" -n "$NS" logs "job/$NAME"
