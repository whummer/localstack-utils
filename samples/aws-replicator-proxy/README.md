# Demo — LocalStack Replicator, AWS Proxy & Cloud Pods

A minimal e-commerce product catalog that illustrates three powerful LocalStack features:

- **AWS Replicator** — mirror real AWS resources (DynamoDB table + Lambda) into LocalStack so you can run the same app locally without re-deploying anything
- **AWS Proxy** — run everything locally, then transparently forward DynamoDB calls to real AWS so your local app sees upstream data
- **Cloud Pods** — snapshot the entire LocalStack state (tables, Lambda, S3, API Gateway) into a versioned pod and restore it in seconds on any machine

## Architecture

```
Browser → S3 Static Website
             ↓ (API URL)
       API Gateway v2 (HTTP API)
             ↓
         Lambda fn   (product-catalog)
             ↓
         DynamoDB    (product-catalog-products)
```

Resources deployed:

| Resource | Name |
|---|---|
| DynamoDB table | `product-catalog-products` |
| Lambda function | `product-catalog` |
| API Gateway v2 | HTTP API with GET/POST/PUT/DELETE routes |
| S3 static website | `product-catalog-frontend-*` |

## Prerequisites

- [LocalStack Pro](https://localstack.cloud) with a valid `LOCALSTACK_AUTH_TOKEN`
- Docker
- LocalStack CLI — `pip install localstack`
- `localstack-extension-aws-replicator` CLI package — `pip install localstack-extension-aws-replicator`
- [Terraform](https://developer.hashicorp.com/terraform/downloads) + [tflocal](https://github.com/localstack/terraform-local) — `pip install terraform-local`
- [AWS CLI](https://aws.amazon.com/cli/) + [awslocal](https://github.com/localstack/awscli-local) — `pip install awscli-local`
- Python 3.10+ with `boto3` — `pip install boto3`
- AWS credentials configured (used by real-AWS steps and the proxy/replicator)

## Quick start

```bash
export LOCALSTACK_AUTH_TOKEN=ls-...   # or add to ~/.bashrc / .env
make localstack-start                 # start LocalStack (auto-installs replicator extension)
make help                             # show all available targets
```

---

## Scenario 1 — AWS Replicator

> Deploy to real AWS first, then replicate the DynamoDB table (with all its items) and the Lambda function into LocalStack. No re-deploy needed locally.

```
Real AWS                        LocalStack
─────────────────               ──────────────────────────
DynamoDB (products)  ──────────►  DynamoDB (replicated)
Lambda function      ──────────►  Lambda function (replicated)
API Gateway / URL               Lambda Function URL (created locally)
```

### Step 1 — Deploy to AWS

```bash
make deploy-aws
```

Terraform creates the DynamoDB table, Lambda, Function URL, S3 website, and IAM role on your real AWS account.

### Step 2 — Seed AWS DynamoDB

```bash
make seed-aws
```

Inserts 4 sample products into the real AWS DynamoDB table.

### Step 3 — Test on AWS

```bash
make test-aws
```

Calls the real AWS Lambda Function URL and verifies the products are returned.

### Step 4 — Replicate into LocalStack

```bash
make replicate
```

This script uses boto3 directly (one client for real AWS, one for LocalStack) to:
1. Copy the DynamoDB table schema and all items
2. Download the Lambda zip from AWS and create the function in LocalStack
3. Wire up an API Gateway in LocalStack pointing at the replicated Lambda
4. Upload `index.html` + `config.json` (with the local API URL) to a local S3 bucket

> **Note:** The `localstack replicator start` CLI does not yet support `AWS::Lambda::Function`, so replication is implemented directly via boto3 here. DynamoDB replication via the CLI also requires AWS credentials inside the LocalStack container, which the direct approach avoids.

The replicated Lambda automatically connects to the replicated local DynamoDB (same table name, LocalStack intercepts).

### Step 5 — Test locally

```bash
make test-local
```

Same 4 products appear — running entirely on LocalStack. No code changes, no re-deploy.

Open the local website printed by `make replicate` and paste in the local API URL to see the frontend.

### Cleanup

```bash
make destroy-aws
```

---

## Scenario 2 — DynamoDB Proxy

> Deploy the entire stack to LocalStack, add local data, then enable the DynamoDB proxy. From that point, all DynamoDB reads/writes go to real AWS — your local Lambda seamlessly uses upstream data.

```
LocalStack
─────────────────────────────────────────────────────────
Browser → S3 → API Gateway → Lambda
                                │
              ┌─────────────────┘
              ▼
        LocalStack DynamoDB ──[proxy enabled]──► Real AWS DynamoDB
```

### Step 1 — Deploy locally

```bash
make deploy-local
```

Deploys the full stack to LocalStack using `tflocal`.

### Step 2 — Seed local DynamoDB

```bash
make seed-local
```

Inserts 4 sample products into the local DynamoDB.

### Step 3 — Test locally (local data)

```bash
make test-local
```

Returns the 4 local products. Everything runs offline.

### Step 4 — Seed real AWS DynamoDB

In a separate terminal, create and seed the AWS DynamoDB table with different products:

```bash
make setup-aws-dynamo
```

This creates the table on real AWS if it doesn't exist, then seeds it with the same 4 products (or different ones if you edited the seed script).

### Step 5 — Enable the DynamoDB proxy

```bash
# Open a NEW terminal for this — it runs in the foreground
make enable-proxy
```

This runs `localstack aws proxy -c proxy_config.yml`, which starts a proxy container that intercepts all DynamoDB calls from LocalStack and forwards them to your real AWS account.

`proxy_config.yml` is configured to proxy **all DynamoDB requests**. You can restrict it to specific tables — see the comments inside.

### Step 6 — Test locally again (now seeing AWS data)

```bash
# Back in your original terminal
make test-local
```

The same local API endpoint now returns data from real AWS DynamoDB. Your local app is transparently using upstream data with zero code changes.

Press `Ctrl+C` in the proxy terminal to stop proxying. Subsequent calls return to the local DynamoDB.

### Cleanup

```bash
make destroy-local
# optionally also clean up the AWS table created by setup-aws-dynamo:
aws dynamodb delete-table --table-name product-catalog-products
```

---

## Scenario 3 — Cloud Pods (snapshot and restore)

> Deploy everything to LocalStack, add products, snapshot the state as a Cloud Pod, then restore it instantly on a fresh instance — no re-deploy needed. Great for sharing a pre-loaded dev environment with your team.

```
LocalStack (populated)
  DynamoDB ─┐
  Lambda   ─┼──[pod save]──► Cloud Pod storage
  S3       ─┘
  API GW   ─┘

                    ↓ make pod-load

LocalStack (fresh) ← full state restored
```

**Prerequisite:** Requires LocalStack Pro (`LOCALSTACK_AUTH_TOKEN` must be set).

### Step 1 — Deploy and seed locally

If you haven't already, deploy the full stack and seed it:

```bash
make deploy-local
make seed-local
```

Use the frontend or `make test-local` to add some extra products via the UI.

### Step 2 — Save state as a Cloud Pod

```bash
make pod-save
# or use a custom name:
# POD_NAME=my-demo-state make pod-save
```

This snapshots the entire LocalStack instance — DynamoDB table (with all items), Lambda function code and config, S3 buckets, API Gateway, IAM roles — everything — into a versioned Cloud Pod stored in the LocalStack cloud platform.

### Step 3 — Wipe and restart LocalStack

```bash
make localstack-stop
make localstack-start
```

After restart, LocalStack is completely empty.

### Step 4 — Restore from the pod

```bash
make pod-load
```

All resources are restored exactly as they were: the DynamoDB table has all items (including anything added via the UI), the Lambda runs the same code, the S3 website is accessible, and the API Gateway routes are live.

```bash
make test-local   # same products as before the restart
```

### Listing and sharing pods

```bash
make pod-list     # show all saved pods
```

Share the pod name with a teammate. They can load it on their own LocalStack instance with `POD_NAME=<name> make pod-load` and get a fully working pre-seeded environment in seconds.

### Cleanup

```bash
localstack pod delete product-catalog-state
make destroy-local
```

---

## File overview

```
.
├── proxy_config.yml        DynamoDB proxy configuration
├── Makefile                All commands for all three scenarios (run 'make help')
├── infra/
│   └── main.tf             Terraform: DynamoDB + Lambda + API Gateway + S3 + IAM
├── lambda/
│   └── handler.py          Lambda handler (GET/POST/PUT/DELETE /products)
├── frontend/
│   └── index.html          S3 static website — list, add, edit, delete products
└── scripts/
    ├── seed.py             Insert sample products into DynamoDB (aws or local)
    ├── replicate.py        Replicate AWS resources into LocalStack
    └── test.py             Smoke-test the API
```

## How the proxy works internally

When `localstack aws proxy -c proxy_config.yml` runs:

1. The LocalStack CLI reads your host AWS credentials and starts a lightweight proxy container
2. The proxy registers itself with the LocalStack server as the handler for DynamoDB requests
3. Your local Lambda calls `http://localhost.localstack.cloud:4566` (the LocalStack DynamoDB endpoint)
4. LocalStack routes matching DynamoDB calls through the proxy container to real AWS
5. Responses flow back through LocalStack to the Lambda — no code change required

The proxy is service-scoped (`dynamodb: {}`), meaning only DynamoDB is proxied. Lambda, S3, and all other services stay local.

## How Cloud Pods work internally

When `localstack pod save <name>` runs:

1. LocalStack serializes the in-memory and on-disk state of every active service
2. The snapshot is uploaded to the LocalStack cloud platform as a versioned, named pod
3. `localstack pod load <name>` downloads the snapshot and injects it into a running instance
4. All resources are re-created in memory — tables, items, Lambda code, S3 objects, routes — exactly as they were

Cloud Pods are version-controlled: each `pod save` creates a new version, and you can `pod load` any specific version. This makes them useful for golden-state environments, regression baselines, and collaborative demos.
