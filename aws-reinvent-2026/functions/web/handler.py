"""Lambda web function (Function URL): serves the Procurement Copilot UI and JSON API."""

import base64
import json
import os
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path

import boto3

ddb = boto3.client("dynamodb")
lam = boto3.client("lambda")

REQUESTS = os.environ["REQUESTS_TABLE"]
BUDGETS = os.environ["BUDGETS_TABLE"]
WORKFLOW = os.environ["WORKFLOW_FUNCTION"]
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()


def plain(av: dict):
    """DynamoDB attribute value -> plain Python."""
    (kind, val), = av.items()
    if kind == "N":
        n = Decimal(val)
        return int(n) if n == n.to_integral() else float(n)
    if kind == "L":
        return [plain(v) for v in val]
    if kind == "M":
        return {k: plain(v) for k, v in val.items()}
    return val


def item(raw: dict) -> dict:
    return {k: plain(v) for k, v in raw.items()}


def respond(code: int, body, content_type="application/json"):
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body)
    if isinstance(body, bytes):
        body = body.decode()
    return {"statusCode": code, "headers": {"content-type": content_type}, "body": body}


def timeline(arn: str) -> list[dict]:
    """Durable execution history -> compact list for the UI."""
    try:
        res = lam.get_durable_execution_history(DurableExecutionArn=arn, IncludeExecutionData=False)
    except Exception as exc:  # execution not visible yet
        return [{"type": f"(history unavailable: {type(exc).__name__})"}]
    out = []
    for ev in res.get("Events", []):
        out.append(
            {
                "type": ev["EventType"],
                "name": ev.get("Name", ""),
                "at": ev["EventTimestamp"].isoformat() if hasattr(ev.get("EventTimestamp"), "isoformat") else str(ev.get("EventTimestamp", "")),
            }
        )
    return out


def create_request(body: dict):
    text = str(body.get("text", "")).strip()
    if not text:
        return respond(400, {"error": "empty request"})
    rid = "req-" + uuid.uuid4().hex[:6]
    flaky = bool(body.get("flaky"))
    now = str(int(time.time() * 1000))
    ddb.put_item(
        TableName=REQUESTS,
        Item={
            "requestId": {"S": rid}, "text": {"S": text}, "status": {"S": "SUBMITTED"},
            "createdAt": {"N": now}, "updatedAt": {"N": now}, "flaky": {"BOOL": flaky},
        },
    )
    # Durable executions are named: the request id doubles as an idempotency key.
    res = lam.invoke(
        FunctionName=WORKFLOW, Qualifier="$LATEST", InvocationType="Event",
        DurableExecutionName=rid,
        Payload=json.dumps({"requestId": rid, "text": text, "flaky": flaky}).encode(),
    )
    ddb.update_item(
        TableName=REQUESTS, Key={"requestId": {"S": rid}},
        UpdateExpression="SET executionArn = :a",
        ExpressionAttributeValues={":a": {"S": res["DurableExecutionArn"]}},
    )
    return respond(202, {"requestId": rid})


def decide(rid: str, body: dict):
    raw = ddb.get_item(TableName=REQUESTS, Key={"requestId": {"S": rid}}).get("Item")
    if not raw or "callbackId" not in raw:
        return respond(409, {"error": "request is not waiting for approval"})
    verdict = {"approved": bool(body.get("approved")), "reason": "rejected by approver"}
    lam.send_durable_execution_callback_success(
        CallbackId=raw["callbackId"]["S"], Result=json.dumps(verdict).encode())
    ddb.update_item(TableName=REQUESTS, Key={"requestId": {"S": rid}}, UpdateExpression="REMOVE callbackId")
    return respond(200, verdict)


def handler(event, context):
    http = event["requestContext"]["http"]
    method, path = http["method"], event["rawPath"]
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return respond(400, {"error": "request body must be JSON"})

    if method == "GET" and path == "/":
        return respond(200, INDEX_HTML, "text/html; charset=utf-8")
    if method == "GET" and path == "/api/budgets":
        rows = [item(r) for r in ddb.scan(TableName=BUDGETS)["Items"]]
        return respond(200, sorted(rows, key=lambda r: r["department"]))
    if method == "GET" and path == "/api/requests":
        rows = [item(r) for r in ddb.scan(TableName=REQUESTS)["Items"]]
        rows.sort(key=lambda r: r["createdAt"], reverse=True)
        for row in rows[:6]:
            row["timeline"] = timeline(row["executionArn"]) if "executionArn" in row else []
        return respond(200, rows[:6])
    if method == "POST" and path == "/api/requests":
        return create_request(data)
    m = re.fullmatch(r"/api/requests/([\w-]+)", path)
    if method == "GET" and m:
        raw = ddb.get_item(TableName=REQUESTS, Key={"requestId": {"S": m.group(1)}}).get("Item")
        if not raw:
            return respond(404, {"error": "not found"})
        row = item(raw)
        row["timeline"] = timeline(row["executionArn"]) if "executionArn" in row else []
        return respond(200, row)
    m = re.fullmatch(r"/api/requests/([\w-]+)/decision", path)
    if method == "POST" and m:
        return decide(m.group(1), data)
    return respond(404, {"error": "not found"})
