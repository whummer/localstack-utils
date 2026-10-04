# Bruin on LocalStack (AWS + Snowflake)

A small [Bruin](https://getbruin.com) data pipeline that runs fully locally against
[LocalStack](https://localstack.cloud). Raw data lives in LocalStack S3, gets loaded and
transformed in the LocalStack Snowflake emulator, and the final report is written back
to LocalStack S3. No cloud accounts needed.

## What the pipeline does

```
 LocalStack S3              LocalStack Snowflake                                   LocalStack S3
 ─────────────              ────────────────────────────────────────────────       ─────────────
 s3://shop-raw   ─stage──►  RAW.CUSTOMERS ─┐
  customers/*.csv  + COPY   RAW.ORDERS    ─┴─► ANALYTICS.ORDERS_ENRICHED ──► ANALYTICS.REVENUE_BY_COUNTRY ──► s3://shop-reports
  orders/*.csv                                   (+ data quality checks)                                      revenue_by_country.json
```

| Asset | File | What it shows |
|---|---|---|
| `raw.shop_data` | `raw_load.py` | Snowflake external stage on a LocalStack S3 bucket, `COPY INTO` from CSV |
| `analytics.orders_enriched` | `orders_enriched.py` | Snowflake CTAS join plus quality checks that fail the run on bad data |
| `analytics.revenue_by_country` | `revenue_by_country.py` | Snowflake aggregation, JSON report published to LocalStack S3 |

Bruin handles the DAG (`depends`), injects the LocalStack connection details as secrets,
and gives each Python asset an isolated environment built with `uv` from
`pipeline/assets/requirements.txt`.

## Prerequisites

- Docker
- A `LOCALSTACK_AUTH_TOKEN` with access to the Snowflake emulator
- [Bruin CLI](https://getbruin.com/docs/bruin/getting-started/introduction/installation.html):
  `curl -LsSf https://getbruin.com/install/cli | sh`
- AWS CLI (used by the Makefile to seed S3)

## Quick start

```bash
export LOCALSTACK_AUTH_TOKEN=ls-...
make all        # start LocalStack, seed S3, run the pipeline, print the report
make stop       # stop and remove the LocalStack container
```

Or step by step:

```bash
make start      # docker compose up (localstack/snowflake image: AWS + Snowflake on :4566)
make seed       # create buckets, upload data/*.csv to s3://shop-raw
make validate   # bruin validate
make lineage    # bruin lineage for the report asset
make run        # bruin run
make report     # print s3://shop-reports/revenue_by_country.json
make test       # assert the report has the expected totals (used in CI)
```

To see the quality checks stop the pipeline, upload an order for a customer that
doesn't exist and run again:

```bash
(cat data/orders.csv; echo "1017,99,2025-06-09,Mouse,1,24.50") | \
  aws --endpoint-url=http://localhost:4566 s3 cp - s3://shop-raw/orders/orders.csv
make run        # analytics.orders_enriched fails, revenue_by_country is skipped
make seed       # restore the original data
```

## How Bruin is pointed at LocalStack

All connections live in [`.bruin.yml`](.bruin.yml), using LocalStack's dummy `test`
credentials.

Bruin's native Snowflake connection has no `host` option (its Go driver always connects
to `<account>.snowflakecomputing.com`), so the Snowflake steps are Python assets. Each one
declares the `localstack-snowflake` connection under `secrets`, Bruin injects it as JSON,
and [`common.py`](pipeline/assets/common.py) connects with `snowflake-connector-python`
using `host=snowflake.localhost.localstack.cloud`, `port=4566`. The S3 endpoint for boto3
comes from a `generic` connection in the same file.
