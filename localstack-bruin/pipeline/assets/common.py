"""Shared helpers for connecting the Python assets to LocalStack (Snowflake + AWS)."""

import json
import os

import boto3
import snowflake.connector


def snowflake_connect(**kwargs):
    # Bruin injects the `localstack-snowflake` connection as JSON (see `secrets` in each asset)
    conn = json.loads(os.environ["SNOWFLAKE_CONN"])
    return snowflake.connector.connect(
        host=os.environ["SNOWFLAKE_HOST"],
        port=4566,
        account=conn["account"],
        user=conn["username"],
        password=conn["password"],
        warehouse=conn.get("warehouse"),
        **kwargs,
    )


def aws_client(service):
    return boto3.client(
        service,
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
