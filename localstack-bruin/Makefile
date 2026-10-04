export AWS_ACCESS_KEY_ID     ?= test
export AWS_SECRET_ACCESS_KEY ?= test
export AWS_DEFAULT_REGION    ?= us-east-1
export LOCALSTACK_PORT       ?= 4566
export SNOWFLAKE_PORT        ?= 4567

AWS   := aws --endpoint-url=http://localhost:$(LOCALSTACK_PORT)
BRUIN := bruin
BRUIN_FLAGS := --config-file .bruin.yml

.PHONY: help start wait seed validate lineage run report test all stop logs

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*##"}{printf "  %-10s %s\n", $$1, $$2}'

start: ## Start LocalStack (S3) and the Snowflake emulator
	docker compose up -d
	@$(MAKE) --no-print-directory wait

wait: ## Wait until LocalStack S3 and the Snowflake emulator are available
	@echo "Waiting for LocalStack..."
	@for i in $$(seq 1 90); do \
	  curl -s localhost:$(LOCALSTACK_PORT)/_localstack/health | grep -q '"s3": "available"' && \
	    curl -s localhost:$(SNOWFLAKE_PORT)/_localstack/health | grep -q '"snowflake":"available"' && exit 0; \
	  if docker compose logs 2>/dev/null | grep -qi "not covered by your license"; then \
	    echo "Your LOCALSTACK_AUTH_TOKEN does not include the Snowflake emulator"; exit 1; fi; \
	  sleep 2; \
	done; echo "Timed out waiting for LocalStack"; exit 1

seed: ## Create S3 buckets and upload the raw CSV files
	$(AWS) s3 mb s3://shop-raw 2>/dev/null || true
	$(AWS) s3 mb s3://shop-reports 2>/dev/null || true
	$(AWS) s3 cp data/customers.csv s3://shop-raw/customers/customers.csv
	$(AWS) s3 cp data/orders.csv s3://shop-raw/orders/orders.csv

validate: ## Validate the Bruin pipeline
	$(BRUIN) validate $(BRUIN_FLAGS) pipeline

lineage: ## Show the lineage of the report asset
	$(BRUIN) lineage --full pipeline/assets/revenue_by_country.py

run: ## Run the full Bruin pipeline
	$(BRUIN) run $(BRUIN_FLAGS) pipeline

report: ## Print the report Snowflake unloaded to S3
	$(AWS) s3 cp s3://shop-reports/revenue_by_country.json -

test: ## Check the report in S3 has the expected totals
	@$(AWS) s3 cp s3://shop-reports/revenue_by_country.json - | python3 -c 'import json, sys; \
	  rows = {r["COUNTRY"]: r for r in map(json.loads, filter(str.strip, sys.stdin))}; \
	  assert len(rows) == 7, f"expected 7 countries, got {len(rows)}"; \
	  assert rows["US"]["ORDERS"] == 4 and float(rows["US"]["REVENUE"]) == 910.0, rows["US"]; \
	  total = round(sum(float(r["REVENUE"]) for r in rows.values()), 2); \
	  assert total == 2372.16, f"unexpected total revenue {total}"; \
	  print(f"OK: {len(rows)} countries, total revenue {total}")'

all: start seed run report ## Start everything, seed data, run the pipeline, show the report

logs: ## Show the container logs
	docker compose logs

stop: ## Stop and remove the containers
	docker compose down
