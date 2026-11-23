# KEDA (cluster add-on, Spec 010)

KEDA is installed into the cluster from its **pinned release manifest**, not Helm, and is not part of
`infra/k8s/base` (it is a cluster-wide add-on that EKS will get from its own install step in Spec 011).

- Version: see `VERSION` (`2.21.1`); the file's SHA-256 is in `SHA256`.
- Install (done by `scripts/k8s_deploy.sh`, which verifies the checksum before applying):
  `curl -sL https://github.com/kedacore/keda/releases/download/v<VERSION>/keda-<VERSION>.yaml -o keda.yaml && shasum -a 256 -c SHA256 && kubectl apply --server-side -f keda.yaml`
- `--server-side` is required: KEDA's CRDs are too large for client-side apply annotations.
- Scaler used: `metrics-api` reading the API's Prometheus-format `di_queue_outstanding` gauge. KEDA's
  built-in `redis` scaler cannot be used because ARQ keeps its queue in a sorted set, not a list.
