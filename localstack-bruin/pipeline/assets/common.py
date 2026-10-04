"""Shared helper for connecting the Python assets to the LocalStack Snowflake emulator."""

import json
import os

import snowflake.connector

DEFAULT_SNOWFLAKE_PORT = 4567


def snowflake_connect(**kwargs):
    # Bruin injects the `localstack-snowflake` connection as JSON (see `secrets` in each asset)
    conn = json.loads(os.environ["SNOWFLAKE_CONN"])
    return snowflake.connector.connect(
        host=os.environ["SNOWFLAKE_HOST"],
        port=int(os.environ.get("SNOWFLAKE_PORT") or DEFAULT_SNOWFLAKE_PORT),
        account=conn["account"],
        user=conn["username"],
        password=conn["password"],
        warehouse=conn.get("warehouse"),
        **kwargs,
    )
