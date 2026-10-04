"""@bruin
name: analytics.orders_enriched
type: python
description: |
  Joins orders with customers in LocalStack Snowflake and computes line revenue,
  then runs a few data quality checks that fail the pipeline if violated.

depends:
  - raw.shop_data

secrets:
  - key: localstack-snowflake
    inject_as: SNOWFLAKE_CONN
  - key: SNOWFLAKE_HOST
  - key: SNOWFLAKE_PORT
@bruin"""

from .common import snowflake_connect

# check name -> query returning the number of offending rows
CHECKS = {
    "order_id is unique": "SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM ORDERS_ENRICHED",
    "every order has a customer": "SELECT COUNT(*) FROM ORDERS_ENRICHED WHERE country IS NULL",
    "revenue is positive": "SELECT COUNT(*) FROM ORDERS_ENRICHED WHERE revenue <= 0",
}


def main():
    with snowflake_connect(database="SHOP") as sf, sf.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS SHOP.ANALYTICS")
        cur.execute("USE SCHEMA SHOP.ANALYTICS")
        cur.execute(
            """CREATE OR REPLACE TABLE ORDERS_ENRICHED AS
               SELECT o.order_id, o.order_date, o.customer_id,
                      c.name AS customer_name, c.country,
                      o.product, o.quantity, o.unit_price,
                      o.quantity * o.unit_price AS revenue
               FROM SHOP.RAW.ORDERS o
               LEFT JOIN SHOP.RAW.CUSTOMERS c ON c.customer_id = o.customer_id"""
        )

        failed = []
        for name, query in CHECKS.items():
            cur.execute(query)
            bad_rows = cur.fetchone()[0]
            print(f"[{'PASS' if bad_rows == 0 else 'FAIL'}] {name}")
            if bad_rows:
                failed.append(name)

    if failed:
        raise SystemExit(f"Data quality checks failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
