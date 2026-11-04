"use strict";
/**
 * Close Copilot frontend: a plain Node.js HTTP server running as an AWS Lambda Web Function.
 * Lambda tells us where to listen through AWS_LAMBDA_HTTP_ENDPOINT ("host:port") and forwards
 * requests as plain HTTP, so there is no event handler and response streaming works out of the box.
 *
 *   GET  /                              the UI
 *   GET  /api/stream                    Server-Sent Events: live close runs, chaos state, held feeds
 *   GET  /api/closes[/:id]              close runs with agent trace and durable timeline
 *   POST /api/close                     start a close (a named durable execution)
 *   POST /api/closes/:id/decision       controller sign-off (completes the durable callback)
 *   GET  /api/logs                      Server-Sent Events: CloudWatch Logs of the workflow, sandbox and notifier
 *   GET  /api/infra                     Server-Sent Events: live inventory of the AWS resources behind the demo
 *   POST /api/chaos                     arm failure injection (kill sandbox, ERP faults)
 *   POST /api/feeds/:entity/deliver     deliver a held bank feed
 */
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const {
  DynamoDBClient, ScanCommand, GetItemCommand, PutItemCommand, UpdateItemCommand,
} = require("@aws-sdk/client-dynamodb");
const { CloudWatchLogsClient, FilterLogEventsCommand } = require("@aws-sdk/client-cloudwatch-logs");
const { SQSClient, GetQueueUrlCommand, GetQueueAttributesCommand } = require("@aws-sdk/client-sqs");
const { S3Client, ListObjectsV2Command, CopyObjectCommand, DeleteObjectCommand, HeadObjectCommand } = require("@aws-sdk/client-s3");
const {
  LambdaClient, InvokeCommand, GetDurableExecutionHistoryCommand, GetFunctionConfigurationCommand,
  ListDurableExecutionsByFunctionCommand,
  SendDurableExecutionCallbackSuccessCommand,
} = require("@aws-sdk/client-lambda");
const { marshall, unmarshall } = require("@aws-sdk/util-dynamodb");

const RUNS = process.env.RUNS_TABLE;
const CONFIG = process.env.CONFIG_TABLE;
const WORKFLOW = process.env.WORKFLOW_FUNCTION;
const BUCKET = process.env.DOCS_BUCKET;
const AGENT_MODE = process.env.AGENT_MODE || "rules";
const AGENT_RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN || "";
const POSTINGS = process.env.POSTINGS_TABLE;
const QUEUE = process.env.NOTIFICATIONS_QUEUE || "close-notifications";
const TOPIC = process.env.NOTIFICATIONS_TOPIC_NAME || "close-notifications";
const PERIOD = process.env.CLOSE_PERIOD || "2026-10";
const STREAM_SECONDS = Number(process.env.STREAM_SECONDS || 120); // clients reconnect automatically
const INDEX_HTML = fs.readFileSync(path.join(__dirname, "index.html"));

// The SDK picks up region, credentials and (on LocalStack) the endpoint from the environment.
const ddb = new DynamoDBClient();
const lambda = new LambdaClient();
const s3 = new S3Client({ forcePathStyle: true });
const logs = new CloudWatchLogsClient();
const sqs = new SQSClient();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const json = (res, code, body) => {
  res.writeHead(code, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const readJson = async (req) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString();
  return raw ? JSON.parse(raw) : {};
};

// Finished executions never change, so cache their history.
const historyCache = new Map();
const DONE = new Set(["ExecutionSucceeded", "ExecutionFailed", "ExecutionTimedOut", "ExecutionStopped"]);

async function timeline(arn) {
  if (historyCache.has(arn)) return historyCache.get(arn);
  try {
    const res = await lambda.send(new GetDurableExecutionHistoryCommand({
      DurableExecutionArn: arn, IncludeExecutionData: false,
    }));
    const events = (res.Events || []).map((e) => ({
      type: e.EventType, name: e.Name || "", at: new Date(e.EventTimestamp).toISOString(),
    }));
    if (events.some((e) => DONE.has(e.type))) historyCache.set(arn, events);
    return events;
  } catch (err) {
    return [{ type: `(history unavailable: ${err.name})` }];
  }
}

const ENTITIES = ["US", "DE", "UK"];
const CURRENCY = { US: "USD", DE: "EUR", UK: "GBP" };

async function listRuns(limit = 3) {
  const { Items = [] } = await ddb.send(new ScanCommand({ TableName: RUNS }));
  const rows = Items.map((i) => unmarshall(i)).sort((a, b) => b.createdAt - a.createdAt).slice(0, limit);
  await Promise.all(rows.map(async (r) => { r.timeline = r.executionArn ? await timeline(r.executionArn) : []; }));
  return rows;
}

async function getChaos() {
  const { Item } = await ddb.send(new GetItemCommand({ TableName: CONFIG, Key: marshall({ key: "chaos" }) }));
  const c = Item ? unmarshall(Item) : {};
  return { killSandbox: Number(c.killSandbox || 0), erpFailures: Number(c.erpFailures || 0), erpDown: Boolean(c.erpDown) };
}

async function setChaos(res, body) {
  const sets = [], vals = {};
  if ("killSandbox" in body) { sets.push("killSandbox = :k"); vals[":k"] = Math.max(0, Number(body.killSandbox) | 0); }
  if ("erpFailures" in body) { sets.push("erpFailures = :f"); vals[":f"] = Math.max(0, Number(body.erpFailures) | 0); }
  if ("erpDown" in body) { sets.push("erpDown = :d"); vals[":d"] = Boolean(body.erpDown); }
  if (sets.length) {
    await ddb.send(new UpdateItemCommand({
      TableName: CONFIG, Key: marshall({ key: "chaos" }),
      UpdateExpression: "SET " + sets.join(", "), ExpressionAttributeValues: marshall(vals),
    }));
  }
  json(res, 200, await getChaos());
}

// A bank feed is "held" while it sits in staging/ and has not reached feeds/ yet.
async function heldFeeds() {
  const held = [];
  for (const e of ENTITIES) {
    const there = await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: `feeds/${PERIOD}/${e}/bank.csv` })).then(() => true, () => false);
    if (!there) held.push(e);
  }
  return held;
}

async function deliverFeed(res, entity) {
  if (!ENTITIES.includes(entity)) return json(res, 404, { error: "unknown entity" });
  const staged = `staging/${PERIOD}/${entity}/bank.csv`;
  await s3.send(new CopyObjectCommand({ Bucket: BUCKET, CopySource: `${BUCKET}/${staged}`, Key: `feeds/${PERIOD}/${entity}/bank.csv` }));
  await s3.send(new DeleteObjectCommand({ Bucket: BUCKET, Key: staged }));
  json(res, 200, { ok: true });
}

async function startClose(res, body) {
  const period = String(body.period || PERIOD);
  const closeId = `close-${period}-${crypto.randomBytes(2).toString("hex")}`;
  const now = Date.now();
  const entities = Object.fromEntries(ENTITIES.map((e) => [e, {
    status: "PENDING", currency: CURRENCY[e], step: "queued", matchRate: 0, matched: 0, unmatched: 0 }]));
  await ddb.send(new PutItemCommand({
    TableName: RUNS,
    Item: marshall({ closeId, period, status: "STARTED", detail: "close started", createdAt: now, updatedAt: now,
                     entities, analysis: {}, trace: [], journals: [], audit: [], totalUsdCents: 0, approval: { required: false } }),
  }));
  // Durable executions are named: the close id doubles as an idempotency key.
  const out = await lambda.send(new InvokeCommand({
    FunctionName: WORKFLOW, Qualifier: "$LATEST", InvocationType: "Event",
    DurableExecutionName: closeId, Payload: Buffer.from(JSON.stringify({ closeId, period })),
  }));
  await ddb.send(new UpdateItemCommand({
    TableName: RUNS, Key: marshall({ closeId }),
    UpdateExpression: "SET executionArn = :a", ExpressionAttributeValues: marshall({ ":a": out.DurableExecutionArn }),
  }));
  json(res, 202, { closeId });
}

async function decide(res, closeId, body) {
  const { Item } = await ddb.send(new GetItemCommand({ TableName: RUNS, Key: marshall({ closeId }) }));
  const row = Item && unmarshall(Item);
  if (!row || !row.callbackId) return json(res, 409, { error: "close is not waiting for sign-off" });
  const verdict = { approved: Boolean(body.approved), approver: String(body.approver || "controller"),
                    reason: "rejected by controller" };
  await lambda.send(new SendDurableExecutionCallbackSuccessCommand({
    CallbackId: row.callbackId, Result: Buffer.from(JSON.stringify(verdict)),
  }));
  await ddb.send(new UpdateItemCommand({
    TableName: RUNS, Key: marshall({ closeId }), UpdateExpression: "REMOVE callbackId",
  }));
  json(res, 200, verdict);
}

// Server-Sent Events: push a snapshot whenever a run, the agent trace, the durable timeline
// or the chaos state changes, so the UI follows the agents live without polling.
async function stream(req, res) {
  res.writeHead(200, {
    "content-type": "text/event-stream", "cache-control": "no-cache, no-transform", "x-accel-buffering": "no",
  });
  let open = true;
  req.on("close", () => { open = false; });
  res.write("retry: 1000\n\n");
  let last = "";
  const deadline = Date.now() + STREAM_SECONDS * 1000;
  while (open && Date.now() < deadline) {
    try {
      const [runs, chaos, held] = await Promise.all([listRuns(), getChaos(), heldFeeds()]);
      const snapshot = JSON.stringify({ runs, chaos, heldFeeds: held, agentMode: AGENT_MODE });
      if (snapshot !== last) {
        last = snapshot;
        res.write(`event: snapshot\ndata: ${snapshot}\n\n`);
      } else {
        res.write(": keep-alive\n\n");
      }
    } catch (err) {
      console.error("stream tick failed", err);
      res.write(`event: error\ndata: ${JSON.stringify({ error: err.name })}\n\n`);
    }
    await sleep(500);
  }
  res.end();
}

// Tail the CloudWatch Logs groups LocalStack writes for each Lambda, so the UI can show what the
// functions print while they run. Durable-SDK JSON log lines are flattened to "LEVEL message".
const LOG_GROUPS = { workflow: "/aws/lambda/close-workflow", sandbox: "/aws/lambda/close-sandbox", notifier: "/aws/lambda/close-notifier" };
const NOISE = /^(START|END|REPORT) RequestId/;

function logLine(raw) {
  const text = String(raw).replace(/\s+$/, "");
  if (NOISE.test(text)) return null;
  if (text.startsWith("{")) {
    try {
      const j = JSON.parse(text);
      if (j.message) return { level: String(j.level || "INFO"), text: String(j.message) };
    } catch { /* plain text line */ }
  }
  return { level: /error|traceback|exception/i.test(text) ? "ERROR" : "INFO", text };
}

async function logs_(req, res) {
  res.writeHead(200, {
    "content-type": "text/event-stream", "cache-control": "no-cache, no-transform", "x-accel-buffering": "no",
  });
  let open = true;
  req.on("close", () => { open = false; });
  res.write("retry: 1000\n\n");
  const since = Date.now() - 20000;
  const cursor = Object.fromEntries(Object.keys(LOG_GROUPS).map((k) => [k, since]));
  const seen = new Set();
  const deadline = Date.now() + STREAM_SECONDS * 1000;
  while (open && Date.now() < deadline) {
    const batch = [];
    await Promise.all(Object.entries(LOG_GROUPS).map(async ([fn, group]) => {
      try {
        const out = await logs.send(new FilterLogEventsCommand({ logGroupName: group, startTime: cursor[fn], limit: 200 }));
        for (const e of out.events || []) {
          cursor[fn] = Math.max(cursor[fn], e.timestamp);
          if (seen.has(e.eventId)) continue;
          seen.add(e.eventId);
          const line = logLine(e.message);
          if (line) batch.push({ fn, ts: e.timestamp, ...line });
        }
      } catch (err) {
        if (err.name !== "ResourceNotFoundException") console.error("log tail failed", fn, err.name);
      }
    }));
    if (seen.size > 5000) seen.clear();
    if (batch.length) {
      batch.sort((a, b) => a.ts - b.ts);
      res.write(`event: logs\ndata: ${JSON.stringify(batch)}\n\n`);
    } else {
      res.write(": keep-alive\n\n");
    }
    await sleep(1000);
  }
  res.end();
}

// Live inventory of what runs behind the demo, for the UI's "Under the hood" panel. Every probe is
// best effort: one slow or failing service must not blank the whole panel.
const LAMBDAS = [
  { name: "close-workflow", kind: "durable" }, { name: "close-sandbox", kind: "sandbox" },
  { name: "close-notifier", kind: "event" },
];
const warned = new Set();
const attempt = async (fn, what = "probe") => {
  try { return await fn(); } catch (err) {
    if (!warned.has(what)) { warned.add(what); console.error(`infra: ${what} failed: ${err.name}: ${err.message}`); }
    return null;
  }
};
const tableCount = (table) => attempt(async () =>
  (await ddb.send(new ScanCommand({ TableName: table, Select: "COUNT" }))).Count, `scan ${table}`);

async function inventory() {
  const [fns, execs, tables, objs, q] = await Promise.all([
    Promise.all(LAMBDAS.map(async (l) => {
      const c = await attempt(() => lambda.send(new GetFunctionConfigurationCommand({ FunctionName: l.name })), `get ${l.name}`);
      return { ...l, runtime: c && c.Runtime, memoryMb: c && c.MemorySize, state: c && c.State, lastModified: c && c.LastModified };
    })),
    attempt(() => lambda.send(new ListDurableExecutionsByFunctionCommand({ FunctionName: WORKFLOW, MaxItems: 200 })), "list durable executions"),
    Promise.all([RUNS, POSTINGS, CONFIG].map(async (t) => ({ table: t, items: await tableCount(t) }))),
    attempt(() => s3.send(new ListObjectsV2Command({ Bucket: BUCKET })), "list s3"),
    attempt(async () => {
      const { QueueUrl } = await sqs.send(new GetQueueUrlCommand({ QueueName: QUEUE }));
      const a = (await sqs.send(new GetQueueAttributesCommand({ QueueUrl, AttributeNames: ["All"] }))).Attributes || {};
      return { queue: QUEUE, visible: Number(a.ApproximateNumberOfMessages || 0), inFlight: Number(a.ApproximateNumberOfMessagesNotVisible || 0) };
    }, "sqs queue attributes"),
  ]);
  const durable = { RUNNING: 0, SUCCEEDED: 0, FAILED: 0, STOPPED: 0, TIMED_OUT: 0 };
  for (const e of (execs && execs.DurableExecutions) || []) durable[e.Status] = (durable[e.Status] || 0) + 1;
  const prefixes = { feeds: 0, staging: 0, packages: 0 };
  for (const o of (objs && objs.Contents) || []) { const p = o.Key.split("/")[0]; if (p in prefixes) prefixes[p]++; }
  return {
    at: Date.now(),
    lambdas: [{ name: "close-web", kind: "web", runtime: "nodejs24.x", state: "Active" }, ...fns.map((f) => ({
      ...f, lastModified: f.lastModified || null }))],
    durable: execs ? durable : null,
    dynamodb: tables,
    s3: objs ? { bucket: BUCKET, prefixes, total: (objs.Contents || []).length } : null,
    sqs: q,
    sns: { topic: TOPIC },
    agentcore: { runtimeArn: AGENT_RUNTIME_ARN, mode: AGENT_MODE },
  };
}

async function infraStream(req, res) {
  res.writeHead(200, {
    "content-type": "text/event-stream", "cache-control": "no-cache, no-transform", "x-accel-buffering": "no",
  });
  let open = true;
  req.on("close", () => { open = false; });
  res.write("retry: 1000\n\n");
  let last = "";
  const deadline = Date.now() + STREAM_SECONDS * 1000;
  while (open && Date.now() < deadline) {
    try {
      const inv = await inventory();
      const body = JSON.stringify({ ...inv, at: 0 });       // `at` excluded so an idle system sends nothing new
      if (body !== last) { last = body; res.write(`event: infra\ndata: ${JSON.stringify(inv)}\n\n`); }
      else res.write(": keep-alive\n\n");
    } catch (err) {
      console.error("infra tick failed", err);
    }
    await sleep(2000);
  }
  res.end();
}

const server = http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, "http://localhost");
  try {
    let m;
    if (req.method === "GET" && pathname === "/") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(INDEX_HTML);
    }
    if (req.method === "GET" && pathname === "/api/stream") return await stream(req, res);
    if (req.method === "GET" && pathname === "/api/infra") return await infraStream(req, res);
    if (req.method === "GET" && pathname === "/api/logs") return await logs_(req, res);
    if (req.method === "GET" && pathname === "/api/closes") return json(res, 200, await listRuns());
    if (req.method === "POST" && pathname === "/api/close") return await startClose(res, await readJson(req));
    if (req.method === "POST" && pathname === "/api/chaos") return await setChaos(res, await readJson(req));
    if (req.method === "GET" && (m = pathname.match(/^\/api\/closes\/([\w-]+)$/))) {
      const { Item } = await ddb.send(new GetItemCommand({ TableName: RUNS, Key: marshall({ closeId: m[1] }) }));
      if (!Item) return json(res, 404, { error: "not found" });
      const row = unmarshall(Item);
      row.timeline = row.executionArn ? await timeline(row.executionArn) : [];
      return json(res, 200, row);
    }
    if (req.method === "POST" && (m = pathname.match(/^\/api\/closes\/([\w-]+)\/decision$/))) {
      return await decide(res, m[1], await readJson(req));
    }
    if (req.method === "POST" && (m = pathname.match(/^\/api\/feeds\/(\w+)\/deliver$/))) return await deliverFeed(res, m[1]);
    json(res, 404, { error: "not found" });
  } catch (err) {
    console.error(err);
    if (!res.headersSent) json(res, err instanceof SyntaxError ? 400 : 500, { error: err.name, message: err.message });
    else res.end();
  }
});

const [host, port] = (process.env.AWS_LAMBDA_HTTP_ENDPOINT || "0.0.0.0:8080").split(":");
server.listen(Number(port), host, () => console.log(`listening on ${host}:${port}`));
