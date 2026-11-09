"""End-to-end tests for Finance Closing Copilot against a deployed stack (run `make deploy` first).

Talks to the Lambda web function over HTTP like the browser does; endpoint and
credentials come from the usual AWS_ENDPOINT_URL / AWS_* environment variables.
"""

import json
import os
import sys
import time
import urllib.request

import boto3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import seed  # noqa: E402  (resets the demo data)


@pytest.fixture(autouse=True)
def fresh_demo_data():
    """Every test starts from the seeded data: no runs, no chaos, DE bank feed held back. No simulated thinking time."""
    seed.main(think_seconds=0)


@pytest.fixture(scope="session")
def base_url():
    cfn = boto3.client("cloudformation")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName="close-copilot")["Stacks"][0]["Outputs"]}
    domain = outputs["WebDomain"]
    # LocalStack reports the endpoint domain with :4566; honour a custom gateway port.
    port = os.environ.get("AWS_ENDPOINT_URL", "").rsplit(":", 1)[-1]
    return "http://" + (domain.replace(":4566", f":{port}") if port.isdigit() else domain)


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


def start(api) -> str:
    return api("POST", "/api/close", {})["closeId"]


def wait_for(api, cid, states, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = api("GET", f"/api/closes/{cid}")
        if row["status"] in states:
            return row
        time.sleep(1)
    raise AssertionError(f"{cid} stuck in {row['status']}: {row.get('detail')}")


def wait_entity(api, cid, entity, status, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = api("GET", f"/api/closes/{cid}")
        if row["entities"][entity]["status"] == status:
            return row
        time.sleep(1)
    raise AssertionError(f"{entity} never reached {status}: {row['entities'][entity]}")


def reach_approval(api):
    """Start a close and walk it through the late DE feed up to the controller's sign-off."""
    cid = start(api)
    wait_entity(api, cid, "DE", "WAITING_FEED")
    api("POST", "/api/feeds/DE/deliver")
    return cid, wait_for(api, cid, {"AWAITING_APPROVAL"})


def test_web_function_serves_ui(base_url):
    html = urllib.request.urlopen(base_url + "/", timeout=30).read().decode()
    assert "Finance Closing Copilot" in html


def test_late_feed_parks_only_that_entity_then_close_completes(api):
    cid = start(api)
    row = wait_entity(api, cid, "DE", "WAITING_FEED")
    assert "bank feed" in row["entities"]["DE"]["step"]
    us = wait_entity(api, cid, "US", "DRAFTED")["entities"]["US"]  # the other entities carry on
    assert us["matched"] > 0
    api("POST", "/api/feeds/DE/deliver")
    row = wait_for(api, cid, {"AWAITING_APPROVAL"})
    assert row["entities"]["DE"]["status"] == "DRAFTED"
    api("POST", f"/api/closes/{cid}/decision", {"approved": True, "approver": "Dana Whitfield, Controller"})
    row = wait_for(api, cid, {"CLOSED", "REJECTED", "ROLLED_BACK"})
    assert row["status"] == "CLOSED"
    assert "ExecutionSucceeded" in [e["type"] for e in row["timeline"]]
    assert any("Dana Whitfield" in a["actor"] for a in row["audit"])
    package = boto3.client("s3").get_object(Bucket="close-documents", Key=f"packages/{cid}.json")
    assert json.loads(package["Body"].read())["closeId"] == cid


def test_agents_run_analysis_in_the_sandbox_and_find_the_planted_exceptions(api):
    cid, row = reach_approval(api)
    assert row["analysis"]["US"]["byType"] == {"timing": 2, "duplicate": 1, "fee": 1, "fx": 0, "unexplained": 0}
    assert row["analysis"]["DE"]["byType"] == {"timing": 1, "duplicate": 0, "fee": 1, "fx": 1, "unexplained": 1}
    assert any(t["kind"] == "sandbox" and t.get("code") for t in row["trace"])
    if os.environ.get("MODEL_MODE", "agentcore") == "agentcore":  # the turns really came from the AgentCore Runtime
        assert all(t["text"].startswith("[agentcore") for t in row["trace"] if t["kind"] == "llm")


def test_policy_overrides_the_agent_on_restricted_accounts(api):
    cid, row = reach_approval(api)
    blocked = [j for j in row["journals"] if j["status"] == "BLOCKED"]
    assert [j["entity"] for j in blocked] == ["DE"] and blocked[0]["creditAccount"] == "3900"
    assert any(t["kind"] == "guardrail" and "policy overrides agent" in t["text"] for t in row["trace"])
    assert row["approval"]["required"] is True


def test_controller_rejection_posts_nothing(api):
    cid, _ = reach_approval(api)
    api("POST", f"/api/closes/{cid}/decision", {"approved": False, "approver": "Dana Whitfield, Controller"})
    row = wait_for(api, cid, {"CLOSED", "REJECTED", "ROLLED_BACK"})
    assert row["status"] == "REJECTED"
    posted = boto3.client("dynamodb").scan(TableName="close-postings")["Items"]
    assert posted == []


def test_killed_sandbox_is_retried_without_repeating_agent_turns(api):
    api("POST", "/api/chaos", {"killSandbox": 1})
    cid, row = reach_approval(api)
    types = [e["type"] for e in row["timeline"]]
    assert types.count("StepFailed") >= 1, "the killed sandbox should show up as a failed, retried step"
    assert any(t["kind"] == "system" and "sandbox died" in t["text"] for t in row["trace"])
    assert api("GET", "/api/closes/" + cid)["analysis"]["US"]["matched"] > 0


def test_flaky_erp_is_retried_by_the_durable_function(api):
    api("POST", "/api/chaos", {"erpFailures": 2})
    cid, _ = reach_approval(api)
    api("POST", f"/api/closes/{cid}/decision", {"approved": True, "approver": "Dana Whitfield, Controller"})
    row = wait_for(api, cid, {"CLOSED", "REJECTED", "ROLLED_BACK"})
    assert row["status"] == "CLOSED"
    assert [e["type"] for e in row["timeline"]].count("StepFailed") >= 2


def test_erp_outage_triggers_saga_reversal(api):
    api("POST", "/api/chaos", {"erpDown": True})
    cid, _ = reach_approval(api)
    api("POST", f"/api/closes/{cid}/decision", {"approved": True, "approver": "Dana Whitfield, Controller"})
    row = wait_for(api, cid, {"CLOSED", "REJECTED", "ROLLED_BACK"}, timeout=180)
    assert row["status"] == "ROLLED_BACK"
    assert any(j["status"] == "REVERSED" for j in row["journals"])
    postings = boto3.client("dynamodb").scan(TableName="close-postings")["Items"]
    assert postings and all(p["status"]["S"] == "REVERSED" for p in postings)


def test_sse_streams_agent_progress_live(api, base_url):
    """The web function streams Server-Sent Events: entity statuses arrive as they happen."""
    stream = urllib.request.urlopen(base_url + "/api/stream", timeout=90)
    cid = start(api)
    seen, event, deadline = [], None, time.time() + 90
    while time.time() < deadline:
        line = stream.readline().decode().rstrip("\n")
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "snapshot":
            snap = json.loads(line[5:])
            run = next((r for r in snap["runs"] if r["closeId"] == cid), None)
            if run and (not seen or seen[-1] != run["entities"]["DE"]["status"]):
                seen.append(run["entities"]["DE"]["status"])
            if "DE" in snap["heldFeeds"] and seen and seen[-1] == "WAITING_FEED":
                api("POST", "/api/feeds/DE/deliver")
            if run and run["entities"]["DE"]["status"] == "DRAFTED":
                break
    stream.close()
    assert "WAITING_FEED" in seen and seen[-1] == "DRAFTED", f"got {seen}"


def test_lambda_logs_are_streamed_to_the_ui(api, base_url):
    """The web function tails CloudWatch Logs of the workflow and sandbox and streams them over SSE."""
    stream = urllib.request.urlopen(base_url + "/api/logs", timeout=90)
    start(api)
    seen, event, deadline = set(), None, time.time() + 90
    while time.time() < deadline and not {"workflow", "sandbox"} <= seen:
        line = stream.readline().decode().rstrip("\n")
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "logs":
            for entry in json.loads(line[5:]):
                assert entry["text"] and not entry["text"].startswith("START RequestId")
                seen.add(entry["fn"])
    stream.close()
    assert {"workflow", "sandbox"} <= seen, f"only saw logs from {seen}"


def test_infra_stream_reports_the_aws_resources_behind_the_demo(api, base_url):
    """The 'Under the hood' panel is fed by a live inventory of the LocalStack resources."""
    stream = urllib.request.urlopen(base_url + "/api/infra", timeout=60)
    event, inv, deadline = None, None, time.time() + 60
    while time.time() < deadline and inv is None:
        line = stream.readline().decode().rstrip("\n")
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "infra":
            inv = json.loads(line[5:])
    stream.close()
    assert inv, "no infra event received"
    assert {l["name"] for l in inv["lambdas"]} == {"close-web", "close-workflow", "close-sandbox", "close-notifier"}
    assert {t["table"] for t in inv["dynamodb"]} == {"close-runs", "close-postings", "close-config"}
    assert inv["s3"]["prefixes"]["staging"] == 1, "the DE bank feed is held in staging/"
    assert inv["s3"]["prefixes"]["feeds"] >= 5  # ledger x3 + bank x2 (DE is held back)
    assert inv["durable"] is not None and inv["sqs"]["queue"] == "close-notifications"
