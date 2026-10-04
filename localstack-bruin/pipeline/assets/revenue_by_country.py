"""@bruin
name: analytics.revenue_by_country
type: python
description: |
  Builds a revenue-by-country mart in LocalStack Snowflake, then publishes it as a
  JSON report to the LocalStack S3 reports bucket.

depends:
  - analytics.orders_enriched

secrets:
  - key: localstack-snowflake
    inject_as: SNOWFLAKE_CONN
  - key: SNOWFLAKE_HOST
  - key: AWS_ENDPOINT_URL
@bruin"""

import json

from .common import aws_client, snowflake_connect

REPORT_BUCKET = "shop-reports"
REPORT_KEY = "revenue_by_country.json"


def main():
    with snowflake_connect(database="SHOP", schema="ANALYTICS") as sf, sf.cursor() as cur:
        cur.execute(
            """CREATE OR REPLACE TABLE REVENUE_BY_COUNTRY AS
               SELECT country,
                      COUNT(DISTINCT customer_id) AS customers,
                      COUNT(*)                    AS orders,
                      ROUND(SUM(revenue), 2)      AS revenue
               FROM ORDERS_ENRICHED
               GROUP BY country"""
        )
        cur.execute("SELECT country, customers, orders, revenue FROM REVENUE_BY_COUNTRY ORDER BY revenue DESC")
        rows = [
            {"country": c, "customers": cu, "orders": o, "revenue": float(r)}
            for c, cu, o, r in cur.fetchall()
        ]

    for row in rows:
        print(f"{row['country']:>4}  orders={row['orders']:<3} revenue={row['revenue']:>9.2f}")

    s3 = aws_client("s3")
    s3.put_object(Bucket=REPORT_BUCKET, Key=REPORT_KEY, Body=json.dumps(rows, indent=2))
    print(f"Report written to s3://{REPORT_BUCKET}/{REPORT_KEY}")


if __name__ == "__main__":
    main()
