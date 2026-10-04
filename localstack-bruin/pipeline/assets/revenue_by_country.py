"""@bruin
name: analytics.revenue_by_country
type: python
description: |
  Builds a revenue-by-country mart in LocalStack Snowflake, then unloads it as a
  JSON report to the LocalStack S3 reports bucket.

depends:
  - analytics.orders_enriched

secrets:
  - key: localstack-snowflake
    inject_as: SNOWFLAKE_CONN
  - key: SNOWFLAKE_HOST
  - key: SNOWFLAKE_PORT
@bruin"""

from .common import snowflake_connect

REPORT_URL = "s3://shop-reports/"
REPORT_FILE = "revenue_by_country.json"


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
        cur.execute("SELECT country, orders, revenue FROM REVENUE_BY_COUNTRY ORDER BY revenue DESC")
        for country, orders, revenue in cur.fetchall():
            print(f"{country:>4}  orders={orders:<3} revenue={revenue:>9.2f}")

        # Unload the mart straight from Snowflake to LocalStack S3 (one JSON object per line)
        cur.execute(
            f"""CREATE OR REPLACE STAGE reports_stage
                  URL = '{REPORT_URL}'
                  CREDENTIALS = (AWS_KEY_ID = 'test' AWS_SECRET_KEY = 'test')"""
        )
        cur.execute(
            f"""COPY INTO @reports_stage/{REPORT_FILE}
                  FROM (SELECT OBJECT_CONSTRUCT(*) FROM REVENUE_BY_COUNTRY ORDER BY revenue DESC)
                  FILE_FORMAT = (TYPE = JSON COMPRESSION = NONE)
                  SINGLE = TRUE OVERWRITE = TRUE"""
        )
    print(f"Report unloaded to {REPORT_URL}{REPORT_FILE}")


if __name__ == "__main__":
    main()
