# LocalStack Sandbox CLI

Experimental CLI tool for managing [LocalStack](https://localstack.cloud) ephemeral (remote) sandbox instances.

Gives AI agents a safe, isolated, stateful cloud environment they can provision, use, and tear down autonomously — without touching real AWS infrastructure.

## The problem

AI agents increasingly run in remote compute environments — containers, VMs, CI runners, cloud-hosted coding assistants — where spinning up a local Docker-based LocalStack instance is impractical or impossible. There's no Docker socket, no persistent filesystem, and no guarantee the environment survives between agent invocations.

LocalStack [ephemeral instances](https://docs.localstack.cloud/user-guide/cloud-sandbox/ephemeral-instance/) are the key enabler here: a fully managed, remote LocalStack environment reachable over HTTPS. The agent doesn't need Docker or any local infrastructure — it just needs network access and a LocalStack account. The sandbox can be provisioned at the start of a session, used like any other LocalStack instance via `AWS_ENDPOINT_URL`, and torn down (with state saved) when the agent is done.

## Patterns

### 1. Sandbox lifecycle management

Agents need to provision their own environment before running AWS workloads, and clean up after. `bin/sandbox` implements this as a simple CLI the agent can call directly:

```
start    — create a fresh ephemeral instance
stop     — delete the instance (state auto-saved to a Cloud Pod)
resume   — recreate the instance and restore saved state
restart  — stop + resume in one step
status   — show current state, age, TTL, and endpoint
reset    — wipe all AWS state without restarting the instance
logs     — fetch instance logs for debugging
url      — emit the endpoint URL for use in scripts
```

The agent exports `AWS_ENDPOINT_URL` once and all subsequent AWS SDK/CLI calls are transparently redirected to the sandbox — no other code changes needed.

### 2. Stateful sessions across agent runs

A key requirement for long-running or multi-step agents is that work survives interruptions. Each sandbox instance is backed by a [Cloud Pod](https://docs.localstack.cloud/user-guide/state-management/cloud-pods/) — a snapshot of the full AWS state (S3 objects, DynamoDB tables, Lambda functions, IAM policies, etc.).

- On `stop`: state is saved automatically via the `pod-on-shutdown` extension
- On `resume`: the pod is loaded before the agent reconnects

This means an agent can be paused mid-task and pick up exactly where it left off in the next session — with the same infrastructure, data, and resource IDs it left behind.

### 3. Isolated environments per agent or task

By parameterising the sandbox name (`--name`), multiple agents or CI jobs can run in parallel without interfering:

```bash
bin/sandbox start --name feature-branch-42
bin/sandbox start --name pr-review-agent
bin/sandbox start --name load-test-run-7
```

Each gets its own endpoint URL, its own Cloud Pod for state persistence, and its own lifetime.

### 4. Debug-mode introspection

When an agent produces unexpected AWS behavior, `--debug` enables trace-level logging on the instance:

```bash
bin/sandbox start --debug
bin/sandbox logs
```

This gives the agent (or a human reviewing the session) full visibility into every AWS API call made during the run.

## bin/sandbox reference

> **Note:** `bin/sandbox` is an experimental CLI included here as a reference implementation. The intent is for this functionality to be integrated directly into the [`localstack` CLI](https://docs.localstack.cloud/getting-started/installation/) over time, at which point this script will no longer be needed.

```
Usage: sandbox <command> [options]

Commands:
  start    Create and start a new ephemeral sandbox instance
  stop     Delete the sandbox (state is auto-saved to a Cloud Pod)
  resume   Recreate the sandbox and restore state from the saved Cloud Pod
  restart  Stop and resume the sandbox (saves and restores state)
  status   Show current status, age, TTL, and endpoint URL
  reset    Reset all AWS state in the running sandbox instance
  logs     Fetch logs from the running sandbox instance
  url      Print the endpoint URL (useful for scripting)

Options:
  --name NAME              Instance name (default: agent-sandbox-<USER>)
  --lifetime MINUTES       Instance lifetime in minutes (default: 60)
  --provider aws|snowflake|azure  Cloud provider to emulate (default: aws)
  --debug                  Enable debug mode (sets DEBUG=1 and LS_LOG=trace)

Environment variables:
  SANDBOX_NAME      Override default instance name
  SANDBOX_LIFETIME  Override default lifetime
  SANDBOX_PROVIDER  Override default provider
```

## Prerequisites

- [LocalStack CLI](https://docs.localstack.cloud/getting-started/installation/) installed and authenticated (`localstack auth login`)
- A LocalStack account with ephemeral instance access

## Using with Claude Code

Add a `CLAUDE.md` at the repo root so the agent self-provisions its environment at the start of every session:

```markdown
## AWS / LocalStack

Before running any AWS commands, check whether a sandbox is already running:

    bin/sandbox status

If not running, start one:

    bin/sandbox start

Set the endpoint for all AWS calls:

    export AWS_ENDPOINT_URL=$(bin/sandbox url | grep https | awk '{print $NF}')

When done, stop the sandbox to save state:

    bin/sandbox stop
```

## Using with other AI agents

Any agent that can execute shell commands can drive this script. The endpoint is a standard HTTPS URL compatible with all AWS SDKs — Python boto3, JavaScript `@aws-sdk`, Go `aws-sdk-go`, etc. — with no changes beyond setting `AWS_ENDPOINT_URL`.
