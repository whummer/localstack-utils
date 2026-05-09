"""
Integration tests: Lambda functions executed by LocalStack's Kubernetes executor.

Prerequisites (handled by `make cluster-up`):
  - k3d cluster 'ls-k8s-cluster' running with NodePort 31566 -> localhost:4566
  - LocalStack deployed via Helm with lambdaExecutor=kubernetes
  - RBAC role allowing LocalStack to create pods in the 'localstack' namespace

Environment overrides:
  LS_ENDPOINT    default http://localhost:4566
  K8S_CONTEXT    default k3d-ls-k8s-cluster
  K8S_NAMESPACE  default localstack
"""

import io
import json
import os
import subprocess
import time
import zipfile

import boto3
import pytest

ENDPOINT = os.environ.get("LS_ENDPOINT", "http://localhost:4566")
REGION = "us-east-1"
CONTEXT = os.environ.get("K8S_CONTEXT", "k3d-ls-k8s-cluster")
NAMESPACE = os.environ.get("K8S_NAMESPACE", "localstack")

# Sleeps long enough that the pod is still Running when we check.
_SLOW_HANDLER = """\
import time

def handler(event, context):
    time.sleep(10)
    return {"statusCode": 200, "body": "hello from k8s lambda"}
"""

_FAST_HANDLER = """\
def handler(event, context):
    return {"statusCode": 200, "body": "hello from k8s lambda"}
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("handler.py", code)
    return buf.getvalue()


def _create_fn(client, name: str, code: str) -> None:
    existing = {f["FunctionName"] for f in client.list_functions()["Functions"]}
    if name in existing:
        return
    client.create_function(
        FunctionName=name,
        Runtime="python3.11",
        Role="arn:aws:iam::000000000000:role/lambda-role",
        Handler="handler.handler",
        Code={"ZipFile": _make_zip(code)},
    )
    waiter = client.get_waiter("function_active_v2")
    waiter.wait(FunctionName=name)


def _pod_names() -> set[str]:
    out = subprocess.check_output(
        ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, "get", "pods", "-o", "json"],
        timeout=10,
    )
    return {p["metadata"]["name"] for p in json.loads(out)["items"]}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def lambda_client():
    return boto3.client(
        "lambda",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_lambda_invoke_returns_correct_payload(lambda_client):
    """Basic smoke test: create + synchronously invoke a Lambda."""
    _create_fn(lambda_client, "test-k8s-fast", _FAST_HANDLER)

    resp = lambda_client.invoke(FunctionName="test-k8s-fast", Payload=json.dumps({}))
    body = json.loads(resp["Payload"].read())

    assert resp["StatusCode"] == 200
    assert body["statusCode"] == 200
    assert "k8s lambda" in body["body"]


def test_lambda_runs_in_dedicated_pod(lambda_client):
    """
    Prove the Kubernetes executor is active: after an async invocation of a
    slow function, a new pod must appear in the LocalStack namespace within 30 s.
    """
    _create_fn(lambda_client, "test-k8s-slow", _SLOW_HANDLER)

    pods_before = _pod_names()

    # Async invocation – pod stays alive for ~10 s while the handler sleeps.
    lambda_client.invoke(
        FunctionName="test-k8s-slow",
        InvocationType="Event",
        Payload=json.dumps({}),
    )

    new_pods: set[str] = set()
    deadline = time.time() + 30
    while time.time() < deadline:
        new_pods = _pod_names() - pods_before
        if new_pods:
            break
        time.sleep(1)

    assert new_pods, (
        f"No new pods appeared in namespace '{NAMESPACE}' within 30 s after Lambda "
        f"invocation. Check that LAMBDA_EXECUTOR=kubernetes is set and that the "
        f"LocalStack ServiceAccount has pod-create RBAC in namespace '{NAMESPACE}'.\n"
        f"Existing pods: {sorted(pods_before)}"
    )
    print(f"\n  Lambda pod(s) observed: {sorted(new_pods)}")
