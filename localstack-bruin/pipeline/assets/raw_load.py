"""@bruin
name: raw.shop_data
type: python
description: |
  Loads the raw CSV files from the LocalStack S3 data lake into LocalStack Snowflake,
  using an external stage on the S3 bucket and COPY INTO.

secrets:
  - key: localstack-snowflake
    inject_as: SNOWFLAKE_CONN
  - key: SNOWFLAKE_HOST
@bruin"""

from .common import snowflake_connect

CSV_FORMAT = "FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1)"


def main():
    with snowflake_connect() as sf, sf.cursor() as cur:
        for stmt in [
            "CREATE DATABASE IF NOT EXISTS SHOP",
            "CREATE SCHEMA IF NOT EXISTS SHOP.RAW",
            "USE SCHEMA SHOP.RAW",
            # External stage pointing at the LocalStack S3 bucket
            """CREATE OR REPLACE STAGE raw_stage
                  URL = 's3://shop-raw/'
                  CREDENTIALS = (AWS_KEY_ID = 'test' AWS_SECRET_KEY = 'test')""",
            """CREATE OR REPLACE TABLE CUSTOMERS (
                  customer_id INT, name VARCHAR, country VARCHAR, signup_date DATE)""",
            """CREATE OR REPLACE TABLE ORDERS (
                  order_id INT, customer_id INT, order_date DATE, product VARCHAR,
                  quantity INT, unit_price NUMBER(10, 2))""",
            f"COPY INTO CUSTOMERS FROM @raw_stage/customers/ {CSV_FORMAT}",
            f"COPY INTO ORDERS FROM @raw_stage/orders/ {CSV_FORMAT}",
        ]:
            cur.execute(stmt)

        for table in ("CUSTOMERS", "ORDERS"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"Loaded {cur.fetchone()[0]} rows into SHOP.RAW.{table}")


if __name__ == "__main__":
    main()
