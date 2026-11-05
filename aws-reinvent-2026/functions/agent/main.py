"""Finance Closing Copilot reconciliation agent as a Bedrock AgentCore Runtime.

The runtime is a stateless brain: given the conversation so far it answers with the NEXT action as JSON.
It never touches a database. Tools, the policy guardrail and every side effect run as checkpointed steps in
the durable function that invokes this runtime.

AgentCore HTTP contract: listen on 0.0.0.0:8080, GET /ping, POST /invocations.

AGENT_MODEL_MODE=rules       deterministic planner (default, fast and repeatable for demo takes)
AGENT_MODEL_MODE=bedrock     ask a model through Bedrock Converse (LocalStack: an Ollama model)
AGENT_MODEL_MODE=openrouter  ask any model on OpenRouter (needs OPENROUTER_API_KEY, OPENROUTER_MODEL optional)
Every model mode falls back to the rule planner when the output is unusable, and says so in the trace.
"""

import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import planner

MODE = os.environ.get("AGENT_MODEL_MODE", "rules")
# Rule mode answers instantly, which looks odd on screen. A model call takes seconds, so the caller can ask for a
# pause per turn ("thinkSeconds" in the request; the workflow takes it from the demo settings). Slightly different
# per entity so the three agents do not move in lockstep. Absent or 0: answer at once.
THINK_SPREAD = {"US": 1.0, "DE": 1.2, "UK": 0.8}
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")


def ask_bedrock(messages: list) -> str:
    import boto3  # bundled only when the agent is packaged for bedrock mode

    res = boto3.client("bedrock-runtime").converse(
        modelId=MODEL_ID, system=[{"text": planner.SYSTEM}],
        messages=[{"role": r, "content": [{"text": t}]} for r, t in messages],
        inferenceConfig={"maxTokens": 300, "temperature": 0})
    return res["output"]["message"]["content"][0]["text"]


def ask_openrouter(messages: list) -> str:
    body = {"model": OPENROUTER_MODEL, "max_tokens": 300, "temperature": 0,
            "messages": [{"role": "system", "content": planner.SYSTEM}] + [{"role": r, "content": t} for r, t in messages]}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read())["choices"][0]["message"]["content"]


ASK = {"bedrock": (ask_bedrock, "agentcore+bedrock"), "openrouter": (ask_openrouter, f"agentcore+openrouter:{OPENROUTER_MODEL}")}


def think(messages: list, think_seconds: float = 0) -> tuple[dict, str]:
    if MODE in ASK:
        ask, label = ASK[MODE]
        try:
            action = planner.extract_json(ask(messages))
            if action and action.get("action") in planner.TOOLS:
                return action, label
            source = f"agentcore+rules (unparseable {MODE} output)"
        except Exception as exc:  # no model available: stay useful
            source = f"agentcore+rules ({type(exc).__name__})"
        return planner.rule_planner(messages), source
    time.sleep(think_seconds * THINK_SPREAD.get(planner.parse_entity(messages[0][1]), 1.0))
    return planner.rule_planner(messages), "agentcore+rules"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            return self._send(200, {"status": "Healthy"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/invocations":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            action, source = think(body["messages"], float(body.get("thinkSeconds") or 0))
            self._send(200, {"action": action, "source": source})
        except Exception as exc:
            self._send(500, {"error": type(exc).__name__, "message": str(exc)})

    def log_message(self, fmt, *args):  # runtime logs go to stdout
        print(fmt % args, flush=True)


if __name__ == "__main__":
    print(f"close agent listening on :8080 (mode={MODE})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
