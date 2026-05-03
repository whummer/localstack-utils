---
name: localstack
displayName: LocalStack – Local AWS Cloud
description: >
  LocalStack is a fully functional local AWS cloud stack that lets you develop
  and test cloud applications entirely on your machine. This power configures
  the LocalStack MCP server and provides guidance for starting LocalStack,
  running AWS CLI commands locally, deploying infrastructure with IaC tools,
  managing state/Cloud Pods, and enforcing IAM policies.
keywords:
  - localstack
  - local aws
  - local cloud
  - aws local
  - cloud emulator
  - aws emulator
  - aws mock
  - local testing
  - local development
  - awslocal
  - tflocal
  - cdklocal
  - cloud pod
  - localstack pro
---

# LocalStack Power

## Onboarding

When a user first mentions LocalStack or any of the trigger keywords, perform
these validation steps:

1. **Check if LocalStack CLI is installed**
   ```bash
   localstack --version
   ```
   If missing, guide the user to install it:
   ```bash
   pip install localstack          # Python / pip
   brew install localstack/tap/localstack-cli  # macOS Homebrew
   ```

2. **Check if LocalStack is running**
   ```bash
   localstack status
   # or
   curl -s http://localhost:4566/_localstack/health | jq .
   ```
   If it's not running, offer to start it:
   ```bash
   localstack start -d             # detached / background
   ```
   For Pro features (IAM enforcement, Cloud Pods, advanced services), set the
   auth token first:
   ```bash
   export LOCALSTACK_AUTH_TOKEN=<token>
   localstack start -d
   ```

3. **Confirm the MCP server is available** – the `localstack-mcp-server`
   defined in `mcp.json` is loaded automatically by Kiro. Use its tools to
   interact with LocalStack whenever they are more convenient than shell
   commands.

---

## Steering Instructions

### Running AWS commands locally

Always prefer the `awslocal` wrapper over the plain `aws` CLI. It automatically
points every request at `http://localhost:4566` with dummy credentials:

```bash
pip install awscli-local          # install once

# Examples
awslocal s3 mb s3://my-bucket
awslocal s3 ls
awslocal dynamodb list-tables
awslocal lambda list-functions
awslocal sqs list-queues
```

Alternatively, configure the standard AWS CLI:
```bash
aws --endpoint-url=http://localhost:4566 s3 ls
```

---

### Lifecycle management

```bash
# Start (detached)
localstack start -d

# Start with debug logging
DEBUG=1 localstack start -d

# Start with persistence across restarts
PERSISTENCE=1 localstack start -d

# Check status & service health
localstack status
curl http://localhost:4566/_localstack/health

# Follow logs
localstack logs -f

# Stop
localstack stop
```

Key environment variables:

| Variable | Description | Default |
|---|---|---|
| `DEBUG` | Verbose debug logging | `0` |
| `PERSISTENCE` | Persist state across restarts | `0` |
| `LOCALSTACK_AUTH_TOKEN` | Pro auth token | – |
| `GATEWAY_LISTEN` | Listen port | `4566` |
| `ENFORCE_IAM` | IAM enforcement mode | disabled |

---

### Infrastructure as Code (IaC)

#### Terraform – use `tflocal` (preferred)

```bash
pip install terraform-local

tflocal init
tflocal plan
tflocal apply -auto-approve
tflocal destroy -auto-approve
```

If `tflocal` is unavailable, add a provider block manually:
```hcl
provider "aws" {
  access_key                  = "test"
  secret_key                  = "test"
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  endpoints {
    s3       = "http://localhost:4566"
    dynamodb = "http://localhost:4566"
    lambda   = "http://localhost:4566"
    # add other services as needed
  }
}
```

#### AWS CDK – use `cdklocal`

```bash
npm install -g aws-cdk-local aws-cdk

cdklocal bootstrap
cdklocal deploy --all --require-approval never
cdklocal destroy --all --force
```

#### CloudFormation – use `awslocal`

```bash
awslocal cloudformation create-stack \
  --stack-name my-stack \
  --template-body file://template.yaml

awslocal cloudformation describe-stacks --stack-name my-stack
awslocal cloudformation delete-stack   --stack-name my-stack
```

#### Pulumi – use `pulumilocal`

```bash
pip install pulumi-local

pulumilocal preview
pulumilocal up --yes
pulumilocal destroy --yes
```

---

### State management

#### Local snapshots (no Pro required)

```bash
localstack state export my-state.zip    # save
localstack state import my-state.zip    # restore
```

Use cases: CI/CD reproducibility, quick backup before destructive changes,
committing fixtures to version control.

#### Cloud Pods (Pro)

Requires `LOCALSTACK_AUTH_TOKEN` to be set.

```bash
localstack pod save   my-pod --message "initial setup"
localstack pod load   my-pod
localstack pod list
localstack pod delete my-pod
```

Use cases: sharing environments across a team, demo-ready snapshots,
cross-machine development.

#### Local persistence (auto-save on restart)

```bash
PERSISTENCE=1 localstack start -d
# State is written to .localstack/ and survives container restarts
```

---

### IAM policy enforcement (Pro)

```bash
# Soft mode – logs violations, allows all requests
ENFORCE_IAM=soft localstack start -d

# Full enforcement – denies unauthorised requests
ENFORCE_IAM=1 localstack start -d
```

Workflow for generating least-privilege policies:
1. Start with `ENFORCE_IAM=soft`
2. Run your application
3. Inspect logs: `localstack logs | grep -i "access denied"`
4. Build a minimal policy covering the observed actions/resources
5. Switch to `ENFORCE_IAM=1` and verify

```bash
# Test a specific action
awslocal iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::000000000000:user/dev-user \
  --action-names s3:GetObject \
  --resource-arns arn:aws:s3:::my-bucket/file.txt
```

---

### Extensions

LocalStack Extensions let you add custom functionality to the LocalStack
container.

```bash
# List installed extensions
localstack extensions list

# Install an extension (Pro)
localstack extensions install localstack-extension-mailhog

# Uninstall
localstack extensions uninstall localstack-extension-mailhog
```

---

### Debugging & logs

```bash
localstack logs -f                      # stream all logs
localstack logs --tail 100              # last 100 lines
localstack logs | grep -i error         # filter errors
localstack logs | grep -i "access denied"  # IAM issues

# Enable verbose debug output at startup
DEBUG=1 localstack start -d
```

Common issues:

| Symptom | Fix |
|---|---|
| Port 4566 already in use | `docker stop localstack-main` or change `GATEWAY_LISTEN` |
| Service returns 404 | Verify the service is in `localstack status` and retry |
| Auth / Pro features missing | Set `LOCALSTACK_AUTH_TOKEN` before starting |
| Container OOM | Increase Docker memory limit (≥4 GB recommended) |

---

### Best practices

- Use `awslocal` / `tflocal` / `cdklocal` / `pulumilocal` wrappers – they
  require zero credential changes in your code.
- Enable `PERSISTENCE=1` during active development to avoid re-creating
  resources after every restart.
- Use Cloud Pods in CI to seed pre-built state rather than running full
  `terraform apply` on every pipeline run.
- Keep `ENFORCE_IAM=soft` during development, switch to `ENFORCE_IAM=1` for
  integration test suites to catch missing permissions early.
- Always use `us-east-1` (or another real region string) – LocalStack ignores
  the region value but some SDKs validate it.
- Use dummy credentials (`access_key = "test"`, `secret_key = "test"`) – they
  are accepted by LocalStack without real AWS access.
