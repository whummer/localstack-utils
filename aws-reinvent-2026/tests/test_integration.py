"""End-to-end tests against a deployed stack (run `make deploy` first).

Talks to the Lambda web function over HTTP like the browser does; endpoint and
credentials come from the usual AWS_ENDPOINT_URL / AWS_* environment variables.
"""

import json
import os
import time
import urllib.request

import boto3
import pytest

AUTO = "Need 5 laptops for the new data engineering hires, around $12k, from Dell."
HUMAN = "Replace the core network switches in the datacenter, about $38k from Cisco Systems."
BLOCKED = "Marketing wants a $9k booth giveaway order from ShadyCorp, urgent."
OVER_LIMIT = "We want to buy $80k of servers from Dell for the engineering team."


BUDGETS = {"engineering": 10_000_000, "it": 6_000_000, "marketing": 2_000_000, "hr": 1_500_000}


@pytest.fixture(autouse=True)
def fresh_budgets():
    """Every test starts from the seeded budgets so tests don't affect each other."""
    ddb = boto3.client("dynamodb")
    for dept, cents in BUDGETS.items():
        ddb.put_item(TableName="procure-budgets", Item={
            "department": {"S": dept}, "remaining_cents": {"N": str(cents)}, "total_cents": {"N": str(cents)}})


@pytest.fixture(scope="session")
def base_url():
    cfn = boto3.client("cloudformation")
    outputs = cfn.describe_stacks(StackName="procurement")["Stacks"][0]["Outputs"]
    return outputs[0]["OutputValue"].rstrip("/")


@pytest.fixture(scope="session")
def api(base_url):
    def call(method, path, body=None):
        req = urllib.request.Request(
            base_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())
    return call


def submit(api, text, flaky=False):
    return api("POST", "/api/requests", {"text": text, "flaky": flaky})["requestId"]


def wait_for(api, rid, states, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = api("GET", f"/api/requests/{rid}")
        if row["status"] in states:
            return row
        time.sleep(1)
    raise AssertionError(f"{rid} stuck in {row['status']}: {row.get('detail')}")


def wait_notification(api, rid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = api("GET", f"/api/requests/{rid}")
        if row.get("notifications"):
            return row
        time.sleep(1)
    raise AssertionError("no notification delivered via SNS -> SQS -> Lambda")


def budget(api, dept):
    return next(b for b in api("GET", "/api/budgets") if b["department"] == dept)["remaining_cents"]


def test_web_function_serves_ui(base_url):
    html = urllib.request.urlopen(base_url + "/", timeout=30).read().decode()
    assert "Procurement Copilot" in html


def test_auto_approve_runs_agent_and_fans_out(api):
    before = budget(api, "engineering")
    rid = submit(api, AUTO)
    row = wait_for(api, rid, {"PO_ISSUED", "REJECTED"})
    assert row["status"] == "PO_ISSUED"
    kinds = [t["kind"] for t in row["trace"]]
    assert kinds.count("tool") == 3 and kinds[-1] == "decision"
    assert "ExecutionSucceeded" in [e["type"] for e in row["timeline"]]
    assert budget(api, "engineering") == before - 1_200_000
    assert "PO-" in wait_notification(api, rid)["notifications"][0]["text"]
    po = boto3.client("s3").get_object(Bucket="procure-documents", Key=f"purchase-orders/PO-{rid[-6:].upper()}.json")
    assert json.loads(po["Body"].read())["amountCents"] == 1_200_000


def test_flaky_erp_is_retried_by_the_durable_function(api):
    rid = submit(api, AUTO, flaky=True)
    row = wait_for(api, rid, {"PO_ISSUED", "REJECTED"})
    assert row["status"] == "PO_ISSUED"
    assert row["erpAttempts"] == 3
    assert [e["type"] for e in row["timeline"]].count("StepFailed") == 2


def test_human_approval_then_po(api):
    rid = submit(api, HUMAN)
    row = wait_for(api, rid, {"AWAITING_APPROVAL"})
    assert "manager" in row["detail"]
    api("POST", f"/api/requests/{rid}/decision", {"approved": True})
    assert wait_for(api, rid, {"PO_ISSUED", "REJECTED"})["status"] == "PO_ISSUED"


def test_human_rejection_does_not_spend_budget(api):
    before = budget(api, "it")
    rid = submit(api, HUMAN)
    wait_for(api, rid, {"AWAITING_APPROVAL"})
    api("POST", f"/api/requests/{rid}/decision", {"approved": False})
    assert wait_for(api, rid, {"PO_ISSUED", "REJECTED"})["status"] == "REJECTED"
    assert budget(api, "it") == before


@pytest.mark.parametrize("text, reason", [(BLOCKED, "not on the approved list"), (OVER_LIMIT, "")])
def test_policy_blocks_what_the_agent_cannot_approve(api, text, reason):
    rid = submit(api, text)
    row = wait_for(api, rid, {"PO_ISSUED", "REJECTED"})
    assert row["status"] == "REJECTED"
    assert reason in row["detail"]
    assert "callbackId" not in row
