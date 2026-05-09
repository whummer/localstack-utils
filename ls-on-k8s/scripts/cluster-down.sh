#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${1:-ls-k8s-cluster}"

echo "==> Deleting k3d cluster '${CLUSTER}'..."
k3d cluster delete "${CLUSTER}"
echo "==> Done."
