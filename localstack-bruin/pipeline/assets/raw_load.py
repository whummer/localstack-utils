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
  - key: SNOWFLAKE_PORT
@bruin"""

from .common import snowflake_connect


def main():
    with snowflake_connect() as sf, sf.cursor() as cur:
        for stmt in [
            "CREATE DATABASE IF NOT EXISTS SHOP",
            "CREATE SCHEMA IF NOT EXISTS SHOP.RAW",
            "USE SCHEMA SHOP.RAW",
            "CREATE OR REPLACE FILE FORMAT csv_format TYPE = CSV PARSE_HEADER = TRUE",
            # External stage pointing at the LocalStack S3 bucket
            """CREATE OR REPLACE STAGE raw_stage
                  URL = 's3://shop-raw/'
                  CREDENTIALS = (AWS_KEY_ID = 'test' AWS_SECRET_KEY = 'test')
                  FILE_FORMAT = csv_format""",
            """CREATE OR REPLACE TABLE CUSTOMERS (
                  customer_id INT, name VARCHAR, country VARCHAR, signup_date DATE)""",
            """CREATE OR REPLACE TABLE ORDERS (
                  order_id INT, customer_id INT, order_date DATE, product VARCHAR,
                  quantity INT, unit_price NUMBER(10, 2))""",
            "COPY INTO CUSTOMERS FROM @raw_stage/customers/ MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE",
            "COPY INTO ORDERS FROM @raw_stage/orders/ MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE",
        ]:
            cur.execute(stmt)

        for table in ("CUSTOMERS", "ORDERS"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"Loaded {cur.fetchone()[0]} rows into SHOP.RAW.{table}")


if __name__ == "__main__":
    main()
