#!/usr/bin/env python3
"""Build the agent image and (re)create its Bedrock AgentCore Runtime. Prints the runtime ARN.

LocalStack runs AgentCore Runtimes from a container image on the same Docker
daemon, so a locally built image is enough (no registry push needed).

    deploy_agent.py            build + create/reuse the runtime, print its ARN
    deploy_agent.py --delete   delete the runtime

Needs LocalStack started with ENABLE_BEDROCK_AGENTCORE=1. Environment:
    AWS_ENDPOINT_URL        LocalStack endpoint
    AGENT_MODEL_MODE        rules (default), bedrock or openrouter: what the agent runtime uses to think
    OPENROUTER_API_KEY      required for openrouter mode (OPENROUTER_MODEL picks the model)
    AGENT_SESSION_MAX_LIFETIME  seconds an agent session container may live (default 300, min 60).
                            LocalStack only implements maxLifetime (idle timeout is a TODO), so this is what
                            keeps idle session containers from piling up
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
NAME = "close_agent"
ROLE = "close-agent-role"
ACCOUNT = "000000000000"

control = boto3.client("bedrock-agentcore-control")
iam = boto3.client("iam")


def find_runtime() -> dict | None:
    for rt in control.list_agent_runtimes().get("agentRuntimes", []):
        if rt["agentRuntimeName"] == NAME:
            return rt
    return None


def build_image() -> str:
    files = [ROOT / "functions/agent/Dockerfile", ROOT / "functions/agent/main.py", ROOT / "functions/common/planner.py"]
    tag = "close-agent:" + hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    with tempfile.TemporaryDirectory() as ctx:
        for f in files:
            shutil.copy(f, ctx)
        subprocess.run(["docker", "build", "-q", "-t", tag, ctx], check=True, stdout=sys.stderr)
    return tag


def ensure_role() -> str:
    try:
        iam.create_role(
            RoleName=ROLE,
            AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}]}),
        )
    except iam.exceptions.EntityAlreadyExistsException:
        pass
    return f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"


def wait_deleted() -> None:
    """Deleting a runtime stops its session containers, which takes a while; creating the replacement
    before that finishes fails because the name is still taken."""
    for _ in range(60):
        if find_runtime() is None:
            return
        time.sleep(1)
    raise SystemExit(f"runtime {NAME} was not deleted in time")


def wait_ready(runtime_id: str) -> None:
    for _ in range(60):
        status = control.get_agent_runtime(agentRuntimeId=runtime_id)
        if status["status"] == "READY":
            return
        if status["status"].endswith("FAILED"):
            raise SystemExit(f"runtime {runtime_id} failed: {status.get('failureReason')}")
        time.sleep(2)
    raise SystemExit(f"runtime {runtime_id} did not become READY")


def main() -> None:
    existing = find_runtime()
    if "--delete" in sys.argv:
        if existing:
            control.delete_agent_runtime(agentRuntimeId=existing["agentRuntimeId"])
        return

    image = build_image()
    mode = os.environ.get("AGENT_MODEL_MODE", "rules")
    env = {"AGENT_MODEL_MODE": mode}
    max_lifetime = max(60, int(os.environ.get("AGENT_SESSION_MAX_LIFETIME", "300")))
    lifecycle = {"idleRuntimeSessionTimeout": 60, "maxLifetime": max_lifetime}
    if mode == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise SystemExit("AGENT_MODEL_MODE=openrouter needs OPENROUTER_API_KEY")
        env["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
        env["OPENROUTER_MODEL"] = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    if existing:
        current = control.get_agent_runtime(agentRuntimeId=existing["agentRuntimeId"])
        same_image = current["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == image
        same_mode = current.get("environmentVariables", {}) == env
        same_lifecycle = current.get("lifecycleConfiguration") == lifecycle
        if same_image and same_mode and same_lifecycle:
            print(existing["agentRuntimeArn"])
            return
        # UpdateAgentRuntime is not emulated yet: replace the runtime instead.
        control.delete_agent_runtime(agentRuntimeId=existing["agentRuntimeId"])
        wait_deleted()

    created = control.create_agent_runtime(
        agentRuntimeName=NAME,
        description="Close agent brain: returns the next action for an entity reconciliation",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
        roleArn=ensure_role(),
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=env,
        lifecycleConfiguration=lifecycle,
    )
    wait_ready(created["agentRuntimeId"])
    print(created["agentRuntimeArn"])


if __name__ == "__main__":
    main()
