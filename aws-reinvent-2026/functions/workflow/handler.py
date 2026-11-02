"""Procurement Copilot: an AI purchasing agent as ONE durable Lambda function.

* Every LLM turn and every tool call is a checkpointed `context.step`: after a
  crash, retry or long human wait the handler replays, but finished LLM calls
  are NOT re-run (no double billing, no divergent re-reasoning).
* "AI proposes, policy disposes": the agent's decision is checked against hard
  rules in code and the stricter outcome wins.
* Humans stay in the loop through a durable callback (no compute while waiting).
* Failures after approval are compensated (saga): the budget is released.
"""

import json
import os
import re
import time

import boto3
from botocore.config import Config
from aws_durable_execution_sdk_python import (
    DurableContext,
    StepContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import CallbackConfig, Duration, JitterStrategy, StepConfig
from aws_durable_execution_sdk_python.exceptions import CallbackError, StepError
from aws_durable_execution_sdk_python.retries import RetryStrategyConfig, create_retry_strategy

ddb = boto3.client("dynamodb")
s3 = boto3.client("s3")
sns = boto3.client("sns")
bedrock = boto3.client("bedrock-runtime", config=Config(read_timeout=90, retries={"max_attempts": 1}))

REQUESTS = os.environ["REQUESTS_TABLE"]
VENDORS = os.environ["VENDORS_TABLE"]
BUDGETS = os.environ["BUDGETS_TABLE"]
BUCKET = os.environ["DOCS_BUCKET"]
TOPIC = os.environ["NOTIFICATIONS_TOPIC"]
MODEL_MODE = os.environ.get("MODEL_MODE", "rules")  # "bedrock" or "rules"
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0")
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "300"))
AUTO_LIMIT = 15_000_00   # cents: up to here the agent may auto-approve
MANAGER_LIMIT = 50_000_00  # up to here a human must approve; above is blocked
MAX_TURNS = 6
ERP_FAILURES = 2
RANK = {"approve": 0, "escalate": 1, "reject": 2}

SYSTEM = """You are a procurement agent. Review the purchase request by calling tools, one per reply.
Reply with ONLY one JSON object, no prose. Allowed:
{"action":"lookup_vendor","args":{"vendor":"<name>"}}
{"action":"check_budget","args":{"department":"engineering|marketing|it|hr","amountCents":<int>}}
{"action":"check_policy","args":{"amountCents":<int>,"category":"hardware|software|services"}}
{"action":"final","args":{"decision":"approve|escalate|reject","reason":"<short>","vendor":"<name>","department":"<dept>","amountCents":<int>,"category":"<category>"}}
Call lookup_vendor, check_budget and check_policy first, then final. Tool results arrive as TOOL_RESULT messages."""


class ErpUnavailable(Exception):
    pass


erp_retry = create_retry_strategy(
    RetryStrategyConfig(
        max_attempts=5,
        initial_delay=Duration.from_seconds(2),
        backoff_rate=2.0,
        jitter_strategy=JitterStrategy.NONE,
        retryable_error_types=[ErpUnavailable],
    )
)


# --------------------------------------------------------------- helpers


def now_ms() -> str:
    return str(int(time.time() * 1000))


def set_status(rid: str, status: str, detail: str | None = None) -> None:
    sets, names = ["#s = :s", "updatedAt = :u"], {"#s": "status"}
    vals = {":s": {"S": status}, ":u": {"N": now_ms()}}
    if detail is not None:
        sets.append("detail = :d")
        vals[":d"] = {"S": detail}
    ddb.update_item(
        TableName=REQUESTS, Key={"requestId": {"S": rid}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
    )


def trace(rid: str, kind: str, text: str) -> None:
    ddb.update_item(
        TableName=REQUESTS, Key={"requestId": {"S": rid}},
        UpdateExpression="SET trace = list_append(if_not_exists(trace, :e), :t)",
        ExpressionAttributeValues={
            ":e": {"L": []},
            ":t": {"L": [{"M": {"kind": {"S": kind}, "text": {"S": text[:400]}}}]},
        },
    )


def money(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def rule_decision(results: dict, amount: int) -> tuple[str, str]:
    """Hard policy. Missing checks mean a human must look."""
    v, b, p = results.get("lookup_vendor"), results.get("check_budget"), results.get("check_policy")
    if not (v and b and p):
        return "escalate", "required checks were skipped by the agent"
    if not v["approved"]:
        return "reject", f"vendor '{v['vendor']}' is not on the approved list"
    if p["tier"] == "blocked":
        return "reject", f"{money(amount)} exceeds the {money(MANAGER_LIMIT)} purchasing limit"
    if not b["sufficient"]:
        return "reject", f"{b['department']} budget has only {money(b['remainingCents'])} left"
    if p["tier"] == "manager":
        return "escalate", f"{money(amount)} needs a manager's approval"
    return "approve", "approved vendor, within budget and auto-approval limit"


# --------------------------------------------------- model + rule planner


def guess_amount(text: str) -> int:
    best = None
    for m in re.finditer(r"(\$)?\s?(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\b", text):
        dollar, num, suffix = m.groups()
        if not (dollar or suffix):
            continue
        val = float(num.replace(",", "")) * {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1)
        best = max(best or 0, val)
    return int((best or 0) * 100)


def guess_department(text: str) -> str:
    t = text.lower()
    for dept, words in {
        "marketing": ["campaign", "booth", "event", "ads", "swag", "brand"],
        "hr": ["training", "recruit", "onboarding", "benefit"],
        "it": ["network", "switch", "firewall", "security", "license", "helpdesk"],
    }.items():
        if any(w in t for w in words):
            return dept
    return "engineering"


def guess_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["license", "subscription", "saas", "software"]):
        return "software"
    if any(w in t for w in ["laptop", "server", "switch", "hardware", "monitor", "router"]):
        return "hardware"
    return "services"


def guess_vendor(text: str) -> str:
    names = [i["name"]["S"] for i in ddb.scan(TableName=VENDORS)["Items"]]
    for name in names:
        if name.lower().split()[0] in text.lower():
            return name
    m = re.search(r"\b(?:from|with|via)\s+([A-Z][\w&.-]*(?:\s[A-Z][\w&.-]*)?)", text)
    return m.group(1) if m else "unknown"


def rule_planner(messages: list[tuple[str, str]]) -> dict:
    text, done = messages[0][1], {}
    for _, t in messages[1:]:
        if t.startswith("TOOL_RESULT "):
            name, _, body = t[len("TOOL_RESULT "):].partition(": ")
            done[name] = json.loads(body)
    amount, dept, cat = guess_amount(text), guess_department(text), guess_category(text)
    if "lookup_vendor" not in done:
        return {"action": "lookup_vendor", "args": {"vendor": guess_vendor(text)}}
    if "check_budget" not in done:
        return {"action": "check_budget", "args": {"department": dept, "amountCents": amount}}
    if "check_policy" not in done:
        return {"action": "check_policy", "args": {"amountCents": amount, "category": cat}}
    decision, reason = rule_decision(done, amount)
    return {"action": "final", "args": {
        "decision": decision, "reason": reason, "vendor": done["lookup_vendor"]["vendor"],
        "department": dept, "amountCents": amount, "category": cat}}


def extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


# ----------------------------------------------------------------- steps


@durable_step
def llm_turn(step: StepContext, rid: str, messages: list) -> dict:
    source, action = "rules", None
    if MODEL_MODE == "bedrock":
        try:
            res = bedrock.converse(
                modelId=MODEL_ID,
                system=[{"text": SYSTEM}],
                messages=[{"role": r, "content": [{"text": t}]} for r, t in messages],
                inferenceConfig={"maxTokens": 300, "temperature": 0},
            )
            raw = res["output"]["message"]["content"][0]["text"]
            cand = extract_json(raw)
            if cand and cand.get("action") in {"lookup_vendor", "check_budget", "check_policy", "final"}:
                action, source = cand, "bedrock"
            else:
                source = "fallback (unparseable model output)"
        except Exception as exc:
            source = f"fallback ({type(exc).__name__})"
    if action is None:
        action = rule_planner(messages)
    trace(rid, "llm", f"[{source}] {json.dumps(action)}")
    return action


@durable_step
def run_tool(step: StepContext, rid: str, action: dict) -> dict:
    name, args = action["action"], action.get("args", {})
    if name == "lookup_vendor":
        want = str(args.get("vendor", "")).lower()
        for item in ddb.scan(TableName=VENDORS)["Items"]:
            if want and (want in item["name"]["S"].lower() or item["name"]["S"].lower().split()[0] in want):
                out = {"found": True, "vendor": item["name"]["S"],
                       "approved": item["approved"]["BOOL"], "riskScore": int(item["risk"]["N"])}
                break
        else:
            out = {"found": False, "vendor": str(args.get("vendor", "unknown")), "approved": False, "riskScore": 100}
    elif name == "check_budget":
        dept, amount = str(args.get("department", "")).lower(), int(args.get("amountCents", 0))
        item = ddb.get_item(TableName=BUDGETS, Key={"department": {"S": dept}}).get("Item")
        left = int(item["remaining_cents"]["N"]) if item else 0
        out = {"department": dept, "remainingCents": left, "sufficient": left >= amount > 0}
    elif name == "check_policy":
        amount = int(args.get("amountCents", 0))
        tier = "auto" if amount <= AUTO_LIMIT else "manager" if amount <= MANAGER_LIMIT else "blocked"
        out = {"tier": tier, "category": args.get("category", "services")}
    else:
        out = {"error": f"unknown tool {name}"}
    trace(rid, "tool", f"{name} -> {json.dumps(out)}")
    return out


@durable_step
def commit_budget(step: StepContext, rid: str, dept: str, amount: int) -> bool:
    try:
        ddb.update_item(
            TableName=BUDGETS, Key={"department": {"S": dept}},
            UpdateExpression="ADD remaining_cents :neg", ConditionExpression="remaining_cents >= :a",
            ExpressionAttributeValues={":neg": {"N": str(-amount)}, ":a": {"N": str(amount)}},
        )
        return True
    except ddb.exceptions.ConditionalCheckFailedException:
        return False


@durable_step
def release_budget(step: StepContext, rid: str, dept: str, amount: int, why: str) -> None:
    ddb.update_item(
        TableName=BUDGETS, Key={"department": {"S": dept}},
        UpdateExpression="ADD remaining_cents :a", ExpressionAttributeValues={":a": {"N": str(amount)}},
    )
    set_status(rid, "REJECTED", why)


@durable_step
def post_to_erp(step: StepContext, rid: str, flaky: bool) -> str:
    res = ddb.update_item(
        TableName=REQUESTS, Key={"requestId": {"S": rid}},
        UpdateExpression="ADD erpAttempts :one", ExpressionAttributeValues={":one": {"N": "1"}},
        ReturnValues="UPDATED_NEW",
    )
    attempt = int(res["Attributes"]["erpAttempts"]["N"])
    if flaky and attempt <= ERP_FAILURES:
        set_status(rid, "ERP_RETRY", f"ERP timed out (attempt {attempt}), backing off")
        raise ErpUnavailable(f"ERP timeout, attempt {attempt}")
    set_status(rid, "POSTING_TO_ERP", f"posted on attempt {attempt}")
    return f"PO-{rid[-6:].upper()}"


@durable_step
def request_approval(step: StepContext, rid: str, callback_id: str, reason: str) -> None:
    ddb.update_item(
        TableName=REQUESTS, Key={"requestId": {"S": rid}},
        UpdateExpression="SET callbackId = :c, #s = :s, detail = :d, updatedAt = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": {"S": callback_id}, ":s": {"S": "AWAITING_APPROVAL"},
                                   ":d": {"S": reason}, ":u": {"N": now_ms()}},
    )


@durable_step
def write_po(step: StepContext, rid: str, po: dict) -> str:
    key = f"purchase-orders/{po['poNumber']}.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(po, indent=2).encode())
    return key


@durable_step
def notify(step: StepContext, rid: str, po: dict) -> None:
    sns.publish(TopicArn=TOPIC, Message=json.dumps({"requestId": rid, **po}))


@durable_step
def finish(step: StepContext, rid: str, status: str, detail: str) -> None:
    set_status(rid, status, detail)


# -------------------------------------------------------------- workflow


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    rid = event["requestId"]
    context.step(lambda _: set_status(rid, "AGENT_REVIEW", "agent is reviewing the request"), name="start-review")

    # --- agent loop: each LLM turn and tool call is a checkpoint
    messages = [("user", event["text"])]
    results, final = {}, None
    for turn in range(MAX_TURNS):
        action = context.step(llm_turn(rid, messages), name=f"llm-turn-{turn}")
        if action["action"] == "final":
            final = action.get("args", {})
            break
        result = context.step(run_tool(rid, action), name=f"tool-{action['action']}")
        results[action["action"]] = result
        messages += [("assistant", json.dumps(action)), ("user", f"TOOL_RESULT {action['action']}: {json.dumps(result)}")]

    # --- guardrail: the stricter of agent and policy wins
    final = final or {"decision": "escalate", "reason": "agent did not reach a decision"}
    amount = int(final.get("amountCents") or guess_amount(event["text"]))
    dept = str(final.get("department") or guess_department(event["text"])).lower()
    rule, rule_reason = rule_decision(results, amount)
    model = final.get("decision", "escalate")
    decision = max([model if model in RANK else "escalate", rule], key=RANK.get)
    reason = final.get("reason", "") if decision == model else rule_reason
    context.step(
        lambda _: trace(rid, "decision",
                        f"agent={model}, policy={rule} -> {decision.upper()}: {reason}"),
        name="guardrail",
    )
    if decision == "reject":
        context.step(finish(rid, "REJECTED", reason), name="reject")
        return {"requestId": rid, "status": "REJECTED"}

    # --- human in the loop (durable callback: zero compute while waiting)
    if decision == "escalate":
        cb = context.create_callback(
            name="human-approval", config=CallbackConfig(timeout=Duration.from_seconds(APPROVAL_TIMEOUT)))
        context.step(request_approval(rid, cb.callback_id, reason), name="request-approval")
        try:
            verdict = json.loads(cb.result() or "{}")
        except CallbackError:
            verdict = {"approved": False, "reason": "approval timed out"}
        if not verdict.get("approved"):
            context.step(finish(rid, "REJECTED", verdict.get("reason", "rejected by approver")), name="rejected-by-human")
            return {"requestId": rid, "status": "REJECTED"}

    # --- commit: budget, ERP (retries), then saga compensation on failure
    if not context.step(commit_budget(rid, dept, amount), name="commit-budget"):
        context.step(finish(rid, "REJECTED", f"{dept} budget exhausted"), name="budget-race")
        return {"requestId": rid, "status": "REJECTED"}
    try:
        po_number = context.step(
            post_to_erp(rid, bool(event.get("flaky"))), name="post-to-erp",
            config=StepConfig(retry_strategy=erp_retry))
    except StepError:
        context.step(release_budget(rid, dept, amount, "ERP unavailable, budget released"), name="compensate")
        return {"requestId": rid, "status": "REJECTED"}

    po = {"poNumber": po_number, "vendor": final.get("vendor"), "department": dept,
          "amountCents": amount, "summary": event["text"][:200]}
    context.parallel(
        [
            lambda ctx: ctx.step(write_po(rid, po), name="write-po-document"),
            lambda ctx: ctx.step(notify(rid, po), name="notify-requester"),
        ],
        name="fulfil",
    )
    context.step(finish(rid, "PO_ISSUED", f"{po_number} issued to {po['vendor']} for {money(amount)}"), name="issue-po")
    return {"requestId": rid, "status": "PO_ISSUED", "po": po_number}
