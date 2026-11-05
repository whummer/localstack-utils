"""Finance Closing Copilot: month-end close for three entities, orchestrated by ONE durable Lambda function.

Every entity gets its own AI reconciliation agent. The agent's brain runs as a Bedrock AgentCore Runtime
(see functions/agent); the code it writes runs in an isolated sandbox function; this function owns the
loop, the tools, the guardrail and every side effect.

* Every LLM turn and every tool call is a checkpointed `context.step`: after a crash, retry or long
  wait the handler replays, but finished LLM calls and sandbox runs are NOT repeated.
* The three entities reconcile in parallel. A missing bank feed parks that entity on a durable wait.
* "AI proposes, policy disposes": drafted journals are checked against hard rules in code (restricted
  accounts, sign-off threshold) and the stricter outcome wins.
* A controller signs off through a durable callback (no compute while waiting).
* Posting to the ERP retries with backoff; if the ERP stays down, a saga reverses what was posted.
"""

import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
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

from planner import (
    ENTITIES, RANK, RECON_CODE, apply_policy, draft_journals, journals_total_usd, money, rule_decision,
    rule_planner,
)

ddb = boto3.client("dynamodb")
s3 = boto3.client("s3")
sns = boto3.client("sns")
lam = boto3.client("lambda", config=Config(read_timeout=120, retries={"max_attempts": 1}))
agentcore = boto3.client("bedrock-agentcore", config=Config(read_timeout=90, retries={"max_attempts": 1}))

RUNS = os.environ["RUNS_TABLE"]
CONFIG = os.environ["CONFIG_TABLE"]
POSTINGS = os.environ["POSTINGS_TABLE"]
BUCKET = os.environ["DOCS_BUCKET"]
TOPIC = os.environ["NOTIFICATIONS_TOPIC"]
SANDBOX = os.environ["SANDBOX_FUNCTION"]
MODEL_MODE = os.environ.get("MODEL_MODE", "agentcore")  # "agentcore" or "rules"
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "600"))
FEED_POLL_SECONDS = int(os.environ.get("FEED_POLL_SECONDS", "4"))
FEED_MAX_POLLS = 120
MAX_TURNS = 6


class ErpUnavailable(Exception):
    pass


class SandboxCrashed(Exception):
    pass


def retry(error_type, attempts, delay):
    return create_retry_strategy(RetryStrategyConfig(
        max_attempts=attempts, initial_delay=Duration.from_seconds(delay), backoff_rate=2.0,
        jitter_strategy=JitterStrategy.NONE, retryable_error_types=[error_type]))


erp_retry = retry(ErpUnavailable, 4, 2)
sandbox_retry = retry(SandboxCrashed, 3, 1)


# --------------------------------------------------------------- helpers

_ser = TypeSerializer()


def ser(value) -> dict:
    return _ser.serialize(json.loads(json.dumps(value), parse_float=Decimal))


def now_ms() -> int:
    return int(time.time() * 1000)


def key(cid: str) -> dict:
    return {"closeId": {"S": cid}}


def set_status(cid: str, status: str, detail: str | None = None) -> None:
    log(f"{cid} -> {status}" + (f" ({detail})" if detail else ""))
    sets, vals = ["#s = :s", "updatedAt = :u"], {":s": {"S": status}, ":u": ser(now_ms())}
    if detail is not None:
        sets.append("detail = :d")
        vals[":d"] = {"S": detail}
    run_update(cid, UpdateExpression="SET " + ", ".join(sets),
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=vals)


def append(cid: str, attr: str, entry: dict) -> None:
    run_update(cid,
                    UpdateExpression=f"SET {attr} = list_append(if_not_exists({attr}, :e), :t)",
                    ExpressionAttributeValues={":e": {"L": []}, ":t": {"L": [ser(entry)]}})


def log(msg: str) -> None:
    """Goes to CloudWatch Logs (and the UI's Logs tab). Only called from steps, so a replay never repeats it."""
    print(msg[:300], flush=True)


def trace(cid: str, entity: str, kind: str, text: str, code: str | None = None) -> None:
    if kind == "llm":  # "[source] {json}" -> "agent [source] -> action", readable in the log pane
        src, _, rest = text.partition("] ")
        try:
            log(f"[{entity}] agent {src}] -> {json.loads(rest)['action']}")
        except (ValueError, KeyError, TypeError):
            log(f"[{entity}] {kind}: {text}")
    else:
        log(f"[{entity}] {kind}: {text}")
    entry = {"entity": entity, "kind": kind, "text": text[:700], "ts": now_ms()}
    if code:
        entry["code"] = code
    append(cid, "trace", entry)


def audit(cid: str, actor: str, event: str) -> None:
    log(f"audit: {actor}: {event}")
    append(cid, "audit", {"at": now_ms(), "actor": actor, "event": event})


class RunGone(Exception):
    """The run row was deleted (demo reset): stop quietly instead of retrying forever."""


def run_update(cid: str, **kwargs) -> None:
    """Update a run row, but never create one: a leftover execution must not resurrect a reset run."""
    try:
        ddb.update_item(TableName=RUNS, Key=key(cid), ConditionExpression="attribute_exists(closeId)", **kwargs)
    except ddb.exceptions.ConditionalCheckFailedException:
        raise RunGone(cid) from None


def set_entity(cid: str, entity: str, **fields) -> None:
    names = {"#e": entity, **{f"#{k}": k for k in fields}}
    vals = {f":{k}": ser(v) for k, v in fields.items()}
    run_update(cid, UpdateExpression="SET " + ", ".join(f"entities.#e.#{k} = :{k}" for k in fields),
               ExpressionAttributeNames=names, ExpressionAttributeValues=vals)


def take_chaos(attr: str) -> bool:
    """Atomically consume one unit of a chaos counter (set from the UI's chaos drawer)."""
    try:
        ddb.update_item(TableName=CONFIG, Key={"key": {"S": "chaos"}},
                        UpdateExpression=f"SET {attr} = {attr} - :one", ConditionExpression=f"{attr} > :zero",
                        ExpressionAttributeValues={":one": {"N": "1"}, ":zero": {"N": "0"}})
        return True
    except ddb.exceptions.ConditionalCheckFailedException:
        return False


def think_seconds() -> float:
    """Demo setting: how long an agent turn pretends to think (seeded as 2.5; tests set 0)."""
    item = ddb.get_item(TableName=CONFIG, Key={"key": {"S": "demo"}}).get("Item", {})
    return float(item.get("thinkSeconds", {}).get("N", 0))


def erp_is_down() -> bool:
    item = ddb.get_item(TableName=CONFIG, Key={"key": {"S": "chaos"}}).get("Item", {})
    return bool(item.get("erpDown", {}).get("BOOL"))


# ----------------------------------------------------------------- steps


@durable_step
def check_feed(step: StepContext, cid: str, period: str, entity: str, polls: int) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=f"feeds/{period}/{entity}/bank.csv")
        return True
    except s3.exceptions.ClientError:
        if polls == 0:  # one line, not one per poll
            log(f"[{entity}] bank feed not there yet, waiting (zero compute)")
        set_entity(cid, entity, status="WAITING_FEED", step="waiting for the bank feed (zero compute)")
        return False


@durable_step
def llm_turn(step: StepContext, cid: str, entity: str, messages: list) -> dict:
    """One agent turn. Checkpointed: a replay never calls the agent (or its model) again."""
    action, source = None, "rules"
    set_entity(cid, entity, status="RECONCILING", step="agent is thinking")
    if MODEL_MODE == "agentcore" and AGENT_RUNTIME_ARN:
        try:
            out = agentcore.invoke_agent_runtime(
                agentRuntimeArn=AGENT_RUNTIME_ARN,
                runtimeSessionId=f"close-session-{cid}-0000000000",  # one session (one runtime container) per close; the brain is stateless (min 33 chars)
                payload=json.dumps({"messages": messages, "thinkSeconds": think_seconds()}).encode(),
                contentType="application/json", accept="application/json",
            )
            answer = json.loads(out["response"].read())
            action, source = answer["action"], answer.get("source", "agentcore")
        except Exception as exc:
            source = f"rules (AgentCore unavailable: {type(exc).__name__})"
    if action is None:
        action = rule_planner(messages)
    trace(cid, entity, "llm", f"[{source}] {json.dumps(action)}")
    set_entity(cid, entity, step=f"agent chose {action['action']}")
    return action


@durable_step
def run_tool(step: StepContext, cid: str, period: str, entity: str, action: dict, results: dict) -> dict:
    name, args = action["action"], action.get("args", {})
    if name == "fetch_feeds":
        set_entity(cid, entity, step="loading feeds from S3")
        keys, counts = {}, {}
        for kind, fname in (("ledger", "ledger.csv"), ("bank", "bank.csv")):
            keys[kind] = f"feeds/{period}/{entity}/{fname}"
            body = s3.get_object(Bucket=BUCKET, Key=keys[kind])["Body"].read()
            counts[kind] = max(body.count(b"\n") - 1, 0)
        out = {"ledgerRows": counts["ledger"], "bankRows": counts["bank"], "keys": keys}
        set_entity(cid, entity, status="RECONCILING", step="feeds loaded")
        trace(cid, entity, "tool", f"fetch_feeds -> ledger {counts['ledger']} rows, bank {counts['bank']} rows")
    elif name == "run_analysis":
        set_entity(cid, entity, step="running code in the sandbox")
        out = analyse(cid, entity, results["fetch_feeds"]["keys"], args.get("code"))
    elif name == "draft_journals":
        set_entity(cid, entity, step="drafting journals")
        analysis = results["run_analysis"]
        journals = draft_journals(entity, ENTITIES[entity], analysis["items"], str(args.get("plugAccount", "")))
        out = {"count": len(journals), "journals": journals}
        trace(cid, entity, "tool", f"draft_journals -> {len(journals)} journal(s) from "
                                   f"{len(analysis['items'])} exceptions (timing differences need none)")
    else:
        out = {"error": f"unknown tool {name}"}
    return out


def analyse(cid: str, entity: str, keys: dict, code: str | None) -> dict:
    """Run the agent's code in the sandbox. A crashed sandbox raises and the step retries on a fresh one."""
    attempts = [(code, "agent-written code")] if code else []
    attempts.append((RECON_CODE, "standard reconciliation"))
    for src, label in attempts:
        payload = {"code": src, "inputs": {k: {"bucket": BUCKET, "key": v} for k, v in keys.items()}}
        if take_chaos("killSandbox"):
            payload["chaos"] = "crash"
        res = lam.invoke(FunctionName=SANDBOX, Payload=json.dumps(payload).encode())
        body = json.loads(res["Payload"].read() or b"{}")
        if res.get("FunctionError"):
            trace(cid, entity, "system", "sandbox died mid-run; retrying on a fresh isolated environment")
            raise SandboxCrashed(body.get("errorMessage", "sandbox crashed"))
        if body.get("ok"):
            analysis = body["result"]
            trace(cid, entity, "sandbox", body["stdout"].strip() or "(no output)", code=src)
            set_entity(cid, entity, matchRate=analysis["matchRate"], matched=analysis["matched"],
                       unmatched=len(analysis["items"]), step="analysis complete")
            run_update(cid, UpdateExpression="SET analysis.#e = :a",
                            ExpressionAttributeNames={"#e": entity},
                            ExpressionAttributeValues={":a": ser({**analysis, "stdout": body["stdout"]})})
            return analysis
        trace(cid, entity, "system", f"{label} failed in the sandbox: {body.get('error', '')[-200:]}")
    raise RuntimeError("analysis failed")


@durable_step
def store_entity_result(step: StepContext, cid: str, entity: str, journals: list, agent: dict) -> dict:
    blocked = [j for j in journals if j["status"] == "BLOCKED"]
    for j in blocked:
        trace(cid, entity, "guardrail",
              f"policy overrides agent: {j['id']} ({j['debitAccount']} / {j['creditAccount']}) -> BLOCKED. {j['reason']}")
        audit(cid, "policy", f"{j['id']} blocked: {j['reason']}")
    audit(cid, f"agent:{entity}", f"drafted {len(journals)} journal(s); {agent.get('reason', '')}")
    set_entity(cid, entity, status="DRAFTED", step=f"{len(journals)} journal(s), {len(blocked)} blocked")
    return {"entity": entity, "journals": journals, "agent": agent}


@durable_step
def record_decision(step: StepContext, cid: str, journals: list, total: int, required: bool, reason: str, line: str) -> None:
    trace(cid, "ALL", "guardrail", line)
    run_update(
        cid,
        UpdateExpression="SET journals = :j, totalUsdCents = :t, approval = :a",
        ExpressionAttributeValues={":j": ser(journals), ":t": ser(total), ":a": ser({"required": required, "reason": reason})})


@durable_step
def request_approval(step: StepContext, cid: str, callback_id: str, reason: str) -> None:
    run_update(
        cid,
        UpdateExpression="SET callbackId = :c, #s = :s, detail = :d, updatedAt = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": {"S": callback_id}, ":s": {"S": "AWAITING_APPROVAL"},
                                   ":d": {"S": reason}, ":u": ser(now_ms())})
    audit(cid, "workflow", f"waiting for controller sign-off: {reason}")


@durable_step
def post_journals(step: StepContext, cid: str, journals: list) -> int:
    """Post to the (mock) ERP. Idempotent per journal. Honors the chaos drawer's ERP faults."""
    todo = [j for j in journals if j["status"] != "BLOCKED"]
    for n, j in enumerate(todo):
        if n >= 1 and (erp_is_down() or take_chaos("erpFailures")):
            set_status(cid, "ERP_RETRY", f"ERP timed out after {n} of {len(todo)} journals, backing off")
            trace(cid, "ALL", "system", f"ERP timeout after posting {n} of {len(todo)} journals")
            raise ErpUnavailable("ERP timeout")
        ddb.put_item(TableName=POSTINGS, Item={
            "journalId": {"S": f"{cid}/{j['id']}"}, "closeId": {"S": cid}, "status": {"S": "POSTED"},
            "amountCents": ser(j["amountCents"]), "entity": {"S": j["entity"]}})
        j["status"] = "POSTED"
    set_status(cid, "POSTING", f"posted {len(todo)} journals to the ERP")
    run_update(cid, UpdateExpression="SET journals = :j",
                    ExpressionAttributeValues={":j": ser(journals)})
    return len(todo)


@durable_step
def reverse_postings(step: StepContext, cid: str, journals: list) -> int:
    """Saga compensation: reverse every journal that made it into the ERP."""
    posted = ddb.scan(TableName=POSTINGS, FilterExpression="closeId = :c",
                      ExpressionAttributeValues={":c": {"S": cid}})["Items"]
    for item in posted:
        ddb.update_item(TableName=POSTINGS, Key={"journalId": item["journalId"]},
                        UpdateExpression="SET #s = :r", ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":r": {"S": "REVERSED"}})
    for j in journals:
        if j["status"] == "POSTED" or any(p["journalId"]["S"].endswith(j["id"]) for p in posted):
            j["status"] = "REVERSED"
    run_update(cid, UpdateExpression="SET journals = :j",
                    ExpressionAttributeValues={":j": ser(journals)})
    trace(cid, "ALL", "system", f"ERP stayed down: saga reversed {len(posted)} posted journal(s)")
    audit(cid, "saga", f"reversed {len(posted)} journal(s) after the ERP failed")
    set_status(cid, "ROLLED_BACK", f"ERP unavailable, {len(posted)} posted journal(s) reversed")
    return len(posted)


@durable_step
def write_package(step: StepContext, cid: str, period: str, journals: list) -> str:
    key_ = f"packages/{cid}.json"
    s3.put_object(Bucket=BUCKET, Key=key_, Body=json.dumps({"closeId": cid, "period": period, "journals": journals}, indent=2).encode())
    return key_


@durable_step
def notify(step: StepContext, cid: str, text: str) -> None:
    sns.publish(TopicArn=TOPIC, Message=json.dumps({"closeId": cid, "text": text}))


@durable_step
def finish(step: StepContext, cid: str, status: str, detail: str, actor: str = "workflow") -> None:
    set_status(cid, status, detail)
    audit(cid, actor, f"{status}: {detail}")


# -------------------------------------------------------------- workflow


def reconcile_entity(ctx: DurableContext, cid: str, period: str, entity: str) -> dict:
    """One entity's branch: wait for the bank feed, then run the agent loop."""
    polls = 0
    while not ctx.step(check_feed(cid, period, entity, polls), name=f"{entity}-check-feed-{polls}"):
        if polls >= FEED_MAX_POLLS:
            raise RuntimeError(f"bank feed for {entity} never arrived")
        ctx.wait(Duration.from_seconds(FEED_POLL_SECONDS), name=f"{entity}-wait-feed-{polls}")
        polls += 1

    messages = [("user", f"Reconcile cash for entity {entity} ({ENTITIES[entity]}), period {period}.")]
    results, final = {}, None
    for turn in range(MAX_TURNS):
        action = ctx.step(llm_turn(cid, entity, messages), name=f"{entity}-llm-turn-{turn}")
        if action["action"] == "final":
            final = action.get("args", {})
            break
        result = ctx.step(run_tool(cid, period, entity, action, results), name=f"{entity}-tool-{action['action']}",
                          config=StepConfig(retry_strategy=sandbox_retry))
        results[action["action"]] = result
        compact = {k: v for k, v in result.items() if k not in ("items", "journals", "keys")}
        messages += [("assistant", json.dumps(action)), ("user", f"TOOL_RESULT {action['action']}: {json.dumps(compact)}")]

    final = final or {"decision": "escalate", "reason": "agent did not reach a decision"}
    journals = apply_policy(results.get("draft_journals", {}).get("journals", []))
    return ctx.step(store_entity_result(cid, entity, journals, final), name=f"{entity}-guardrail")


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    cid, period = event["closeId"], event.get("period", "2026-10")
    context.step(lambda _: audit(cid, "scheduler", f"month-end close started for {period}"), name="audit-start")
    context.step(lambda _: set_status(cid, "RECONCILING", "agents are reconciling the three entities"), name="start")

    # --- one agent per entity, in parallel; a late bank feed parks only that entity
    branches = [(lambda e: (lambda ctx: reconcile_entity(ctx, cid, period, e)))(e) for e in ENTITIES]
    batch = context.parallel(branches, name="reconcile-entities")
    batch.throw_if_error()
    entity_results = batch.get_results()
    journals = [j for r in entity_results for j in r["journals"]]

    # --- guardrail: the stricter of agent and policy wins
    model = max((r["agent"].get("decision", "escalate") for r in entity_results),
                key=lambda d: RANK.get(d, RANK["escalate"]))
    rule, rule_reason = rule_decision(journals)
    decision = max([model if model in RANK else "escalate", rule], key=RANK.get)
    total = journals_total_usd(journals)
    reason = rule_reason if RANK[rule] >= RANK.get(model, 1) else "agent asked for review"
    context.step(record_decision(cid, journals, total, decision == "escalate", reason,
                                 f"agent={model}, policy={rule} -> {decision.upper()}: {reason}"),
                 name="guardrail")
    if decision == "reject":
        context.step(finish(cid, "REJECTED", reason), name="reject")
        return {"closeId": cid, "status": "REJECTED"}

    # --- human in the loop (durable callback: zero compute while waiting)
    if decision == "escalate":
        cb = context.create_callback(
            name="controller-signoff", config=CallbackConfig(timeout=Duration.from_seconds(APPROVAL_TIMEOUT)))
        context.step(request_approval(cid, cb.callback_id, reason), name="request-approval")
        try:
            verdict = json.loads(cb.result() or "{}")
        except CallbackError:
            verdict = {"approved": False, "reason": "sign-off timed out", "approver": "system"}
        who = verdict.get("approver", "controller")
        if not verdict.get("approved"):
            context.step(finish(cid, "REJECTED", verdict.get("reason", "rejected by controller"), who), name="rejected-by-human")
            return {"closeId": cid, "status": "REJECTED"}
        context.step(lambda _: audit(cid, who, f"approved {len(journals)} journal(s), total {money(total)}"), name="audit-approval")

    # --- post to the ERP (retries), then saga compensation on failure
    try:
        posted = context.step(post_journals(cid, journals), name="post-to-erp", config=StepConfig(retry_strategy=erp_retry))
    except StepError:
        context.step(reverse_postings(cid, journals), name="compensate")
        return {"closeId": cid, "status": "ROLLED_BACK"}
    context.step(lambda _: audit(cid, "erp", f"posted {posted} journal(s)"), name="audit-posted")

    context.parallel(
        [
            lambda ctx: ctx.step(write_package(cid, period, [{**j, "status": "POSTED" if j["status"] != "BLOCKED" else j["status"]} for j in journals]),
                                 name="write-close-package"),
            lambda ctx: ctx.step(notify(cid, f"{period} close complete: {posted} journals posted ({money(total)})"),
                                 name="notify-finance"),
        ],
        name="fulfil",
    )
    skipped = sum(1 for j in journals if j["status"] == "BLOCKED")
    context.step(finish(cid, "CLOSED", f"{posted} journals posted, {skipped} held back by policy"), name="close-books")
    return {"closeId": cid, "status": "CLOSED", "posted": posted}
