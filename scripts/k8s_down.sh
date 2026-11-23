#!/usr/bin/env bash
# WHAT:  Deletes the local kind cluster (and with it every pod, PVC and Secret in it).
# WHERE: Repo root: `bash scripts/k8s_down.sh`.
# WHY:   The cluster is disposable by design: state lives in PVCs inside the kind node, so
#        deleting the cluster is a clean reset and leaves nothing running on the laptop.
set -euo pipefail
kind delete cluster --name deal-intel
