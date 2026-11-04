"""Analysis sandbox: runs code written by the agent in an isolated Lambda execution environment.

This is the agent's "run_analysis" tool backend. Isolation comes from the platform: the function has its own
role (read-only access to the feeds bucket, nothing else), no network egress is needed, and every invocation
is a fresh, disposable environment. The interface (code + input files in, stdout + result out) is deliberately
small so the backend can move to a Lambda MicroVM later without touching the workflow.

event: {"code": str, "inputs": {"name": {"bucket": str, "key": str}}, "chaos": "crash" | None}
"""

import contextlib
import io
import json
import os
import tempfile
import traceback

import boto3

s3 = boto3.client("s3")


def handler(event, context):
    if event.get("chaos") == "crash":
        print("sandbox: simulated crash requested, dying mid-run", flush=True)
        os._exit(1)  # simulate the sandbox dying mid-run (OOM kill, host loss)

    work = tempfile.mkdtemp()
    paths = {}
    for name, ref in event.get("inputs", {}).items():
        paths[name] = os.path.join(work, f"{name}.csv")
        s3.download_file(ref["bucket"], ref["key"], paths[name])

    print(f"sandbox: running {len(event['code'].splitlines())} lines of agent code on {sorted(paths)}", flush=True)
    out, result = io.StringIO(), {}
    scope = {"inputs": paths, "result": result}
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(event["code"], "<agent-code>", "exec"), scope)  # noqa: S102 - this IS the sandbox
        print(f"sandbox: finished ok, output: {out.getvalue().strip()[:200]}", flush=True)
        return {"ok": True, "stdout": out.getvalue()[-4000:], "result": json.loads(json.dumps(result))}
    except Exception:
        print("sandbox: agent code failed", flush=True)
        return {"ok": False, "stdout": out.getvalue()[-2000:], "error": traceback.format_exc()[-2000:]}
