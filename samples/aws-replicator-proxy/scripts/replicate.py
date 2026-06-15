#!/usr/bin/env python3
"""Replicate AWS resources (DynamoDB table + Lambda function) into a running LocalStack instance.

The LocalStack replicator extension does not yet support AWS::Lambda::Function, and its
DynamoDB support requires AWS credentials inside the container. This script implements
replication directly via boto3 instead — one client for reading from real AWS, one for
writing to LocalStack — which is more transparent and reliable for a sample.

Usage:
    python scripts/replicate.py

Environment variables (all optional):
    APP_NAME        application name prefix  (default: product-catalog)
    TABLE_NAME      DynamoDB table name      (default: <APP_NAME>-products)
    FUNCTION_NAME   Lambda function name     (default: <APP_NAME>)
    AWS_REGION      AWS region               (default: us-east-1)
    AWS_ENDPOINT_URL LocalStack endpoint     (default: http://localhost:4566)
"""

import json
import os
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

APP_NAME      = os.environ.get("APP_NAME",        "product-catalog")
TABLE_NAME    = os.environ.get("TABLE_NAME",      f"{APP_NAME}-products")
FUNCTION_NAME = os.environ.get("FUNCTION_NAME",    APP_NAME)
AWS_REGION    = os.environ.get("AWS_REGION",      "us-east-1")
LS_ENDPOINT   = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")

FRONTEND_HTML = Path(__file__).parent.parent / "frontend" / "index.html"
BUCKET        = f"{APP_NAME}-frontend-local"


def aws_client(service: str):  # type: ignore[return]
    """Real-AWS boto3 client (uses host credential chain, no endpoint override)."""
    return boto3.client(service, region_name=AWS_REGION)


def local_client(service: str):  # type: ignore[return]
    """LocalStack boto3 client — always uses fake credentials so all resources land
    in the standard 000000000000 namespace that awslocal / the Lambda env-override use."""
    return boto3.client(
        service,
        endpoint_url=LS_ENDPOINT,
        region_name=AWS_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


# ── DynamoDB ───────────────────────────────────────────────────────────────────

def replicate_dynamodb() -> None:
    print(f"==> Replicating DynamoDB table '{TABLE_NAME}'...")
    aws   = aws_client("dynamodb")
    local = local_client("dynamodb")

    table = aws.describe_table(TableName=TABLE_NAME)["Table"]

    create_kwargs: dict = {
        "TableName":            table["TableName"],
        "AttributeDefinitions": table["AttributeDefinitions"],
        "KeySchema":            table["KeySchema"],
        "BillingMode":          "PAY_PER_REQUEST",
    }
    if table.get("GlobalSecondaryIndexes"):
        create_kwargs["GlobalSecondaryIndexes"] = [
            {k: v for k, v in gsi.items() if k in ("IndexName", "KeySchema", "Projection")}
            for gsi in table["GlobalSecondaryIndexes"]
        ]

    try:
        local.create_table(**create_kwargs)
        print(f"  Created table '{TABLE_NAME}' in LocalStack")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"  Table '{TABLE_NAME}' already exists, skipping creation")
        else:
            raise

    local.get_waiter("table_exists").wait(TableName=TABLE_NAME)

    paginator = aws.get_paginator("scan")
    total = 0
    for page in paginator.paginate(TableName=TABLE_NAME):
        for item in page.get("Items", []):
            local.put_item(TableName=TABLE_NAME, Item=item)
            total += 1
    print(f"  Copied {total} item(s) to LocalStack")


# ── Lambda ─────────────────────────────────────────────────────────────────────

def replicate_lambda() -> None:
    print(f"\n==> Replicating Lambda function '{FUNCTION_NAME}'...")
    aws   = aws_client("lambda")
    local = local_client("lambda")

    fn  = aws.get_function(FunctionName=FUNCTION_NAME)
    cfg = fn["Configuration"]

    print("  Downloading function code from AWS...")
    with urllib.request.urlopen(fn["Code"]["Location"]) as r:
        zip_bytes = r.read()

    # Merge in fake LocalStack credentials so the Lambda uses account 000000000000,
    # matching the namespace that local_client() and awslocal create resources under.
    # Without this, LocalStack injects the host's real AWS credentials into the container,
    # causing a cross-account namespace mismatch where the Lambda cannot find local tables.
    env_vars = dict(cfg.get("Environment", {}).get("Variables", {}))
    env_vars.setdefault("AWS_ACCESS_KEY_ID",     "test")
    env_vars.setdefault("AWS_SECRET_ACCESS_KEY",  "test")

    create_kwargs: dict = {
        "FunctionName": FUNCTION_NAME,
        "Runtime":      cfg["Runtime"],
        "Role":         cfg["Role"],
        "Handler":      cfg["Handler"],
        "Code":         {"ZipFile": zip_bytes},
        "Timeout":      cfg.get("Timeout", 30),
        "MemorySize":   cfg.get("MemorySize", 128),
        "Environment":  {"Variables": env_vars},
    }

    try:
        local.create_function(**create_kwargs)
        print(f"  Created function '{FUNCTION_NAME}' in LocalStack")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            local.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=zip_bytes)
            local.update_function_configuration(
                FunctionName=FUNCTION_NAME,
                Handler=cfg["Handler"],
                Runtime=cfg["Runtime"],
                Timeout=cfg.get("Timeout", 30),
                Environment={"Variables": env_vars},
            )
            print(f"  Updated existing function '{FUNCTION_NAME}' in LocalStack")
        else:
            raise

    local.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)


# ── API Gateway ────────────────────────────────────────────────────────────────

ROUTES = (
    "GET /products",
    "POST /products",
    "OPTIONS /products",
    "PUT /products/{id}",
    "DELETE /products/{id}",
    "OPTIONS /products/{id}",
)


def create_api_gateway() -> str:
    """Create (or update) an HTTP API Gateway in LocalStack pointing at the Lambda."""
    print(f"\n==> Creating API Gateway (HTTP API) in LocalStack...")
    apigw = local_client("apigatewayv2")
    lam   = local_client("lambda")

    existing = [a for a in apigw.get_apis().get("Items", []) if a["Name"] == APP_NAME]
    if existing:
        api_id = existing[0]["ApiId"]
        print(f"  API '{APP_NAME}' already exists ({api_id})")
        integ_id = apigw.get_integrations(ApiId=api_id)["Items"][0]["IntegrationId"]
    else:
        api    = apigw.create_api(Name=APP_NAME, ProtocolType="HTTP")
        api_id = api["ApiId"]

        fn_arn = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"]
        integ  = apigw.create_integration(
            ApiId                = api_id,
            IntegrationType      = "AWS_PROXY",
            IntegrationUri       = fn_arn,
            PayloadFormatVersion = "2.0",
        )
        integ_id = integ["IntegrationId"]
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
        print(f"  Created API Gateway '{APP_NAME}' ({api_id})")

    existing_routes = {r["RouteKey"] for r in apigw.get_routes(ApiId=api_id).get("Items", [])}
    for route_key in ROUTES:
        if route_key not in existing_routes:
            apigw.create_route(ApiId=api_id, RouteKey=route_key,
                               Target=f"integrations/{integ_id}")
            print(f"  Added route: {route_key}")

    return f"http://{api_id}.execute-api.localhost.localstack.cloud:4566"


# ── S3 frontend ────────────────────────────────────────────────────────────────

def upload_frontend(local_api: str) -> str:
    print(f"\n==> Uploading frontend to LocalStack S3 bucket '{BUCKET}'...")
    s3 = local_client("s3")

    try:
        s3.create_bucket(Bucket=BUCKET)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise

    s3.put_bucket_website(Bucket=BUCKET,
                          WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": "*",
                       "Action": "s3:GetObject",
                       "Resource": f"arn:aws:s3:::{BUCKET}/*"}],
    }))
    s3.put_object(Bucket=BUCKET, Key="index.html",
                  Body=FRONTEND_HTML.read_bytes(), ContentType="text/html")
    s3.put_object(Bucket=BUCKET, Key="config.json",
                  Body=json.dumps({"apiUrl": local_api}).encode(),
                  ContentType="application/json")

    return f"http://{BUCKET}.s3-website.localhost.localstack.cloud:4566"


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    replicate_dynamodb()
    replicate_lambda()
    local_api = create_api_gateway()
    website   = upload_frontend(local_api)

    print("\n" + "=" * 52)
    print("  Replication complete!")
    print("=" * 52)
    print(f"\n  Local API:      {local_api}/products")
    print(f"  Local website:  {website}")
    print(f"\n  Quick test:")
    print(f"    curl '{local_api}/products'")
    print(f"    python scripts/test.py '{local_api}'")


if __name__ == "__main__":
    main()
