#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${1:-ls-k8s-cluster}"
NODE_PORT="${2:-31566}"
HOST_PORT="${3:-4566}"
NAMESPACE="localstack"
CONTEXT="k3d-${CLUSTER}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Creating k3d cluster '${CLUSTER}'..."
if k3d cluster list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "${CLUSTER}"; then
  echo "    Cluster '${CLUSTER}' already exists – skipping creation."
else
  k3d cluster create "${CLUSTER}" \
    --port "${HOST_PORT}:${NODE_PORT}@server:0" \
    --wait
fi

echo "==> Ensuring namespace '${NAMESPACE}'..."
kubectl --context "${CONTEXT}" create namespace "${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl --context "${CONTEXT}" apply -f -

if [[ -n "${LOCALSTACK_AUTH_TOKEN:-}" ]]; then
  echo "==> Creating auth token secret..."
  kubectl --context "${CONTEXT}" create secret generic localstack-auth \
    --namespace "${NAMESPACE}" \
    --from-literal=token="${LOCALSTACK_AUTH_TOKEN}" \
    --dry-run=client -o yaml | kubectl --context "${CONTEXT}" apply -f -
fi

echo "==> Deploying LocalStack..."
kubectl --context "${CONTEXT}" apply -f "${SCRIPT_DIR}/../k8s/localstack.yaml"

echo "==> Waiting for LocalStack to be ready..."
kubectl --context "${CONTEXT}" rollout status deployment/localstack \
  --namespace "${NAMESPACE}" --timeout=180s

echo "==> Waiting for LocalStack health endpoint on localhost:${HOST_PORT}..."
for i in $(seq 1 60); do
  if curl -sf "http://localhost:${HOST_PORT}/_localstack/health" >/dev/null 2>&1; then
    echo "    LocalStack is ready!"
    echo ""
    echo "    Endpoint : http://localhost:${HOST_PORT}"
    echo "    Context  : ${CONTEXT}"
    echo "    Namespace: ${NAMESPACE}"
    exit 0
  fi
  echo "    Attempt ${i}/60 – waiting 3s..."
  sleep 3
done

echo "ERROR: LocalStack did not become healthy within ~3 min." >&2
echo "Check pod logs: kubectl --context ${CONTEXT} -n ${NAMESPACE} logs deploy/localstack" >&2
exit 1
