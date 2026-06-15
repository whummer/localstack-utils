AWS_REGION    ?= us-east-1
APP_NAME      ?= product-catalog
TABLE_NAME     = $(APP_NAME)-products
FUNCTION_NAME  = $(APP_NAME)
POD_NAME      ?= $(APP_NAME)-state
PYTHON         = python3

# S3 bucket suffix — use your AWS account ID on real AWS to ensure global uniqueness
ACCOUNT_ID    := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "local")

# Resolve the local API Gateway invoke URL without requiring TF state
LOCAL_API     = $(shell awslocal apigatewayv2 get-apis \
                  --query "Items[?Name=='$(APP_NAME)'].ApiEndpoint" \
                  --output text 2>/dev/null | tr -d '\n')

.DEFAULT_GOAL := help

.PHONY: help \
        localstack-start localstack-stop \
        deploy-aws seed-aws test-aws destroy-aws \
        replicate patch-local-lambda \
        deploy-local seed-local setup-aws-dynamo enable-proxy test-local destroy-local \
        pod-save pod-load pod-list

# ── Help ───────────────────────────────────────────────────────────────────────

help:  ## Show available targets
	@awk 'BEGIN {FS = ":.*##"}; \
	     /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }; \
	     /^[a-zA-Z_-]+:.*?## / { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' \
	$(MAKEFILE_LIST)
	@echo ""

##@ LocalStack

localstack-start:  ## Start LocalStack (Pro required for proxy feature)
	EXTRA_CORS_ALLOWED_ORIGINS=https://aws-replicator.localhost.localstack.cloud:4566 \
	GATEWAY_SERVER=hypercorn \
	localstack start -d
	@echo "Waiting for LocalStack..."
	@until localstack status services 2>/dev/null | grep -q running; do sleep 1; done
	@echo "LocalStack is ready."

localstack-stop:  ## Stop LocalStack
	localstack stop

##@ Scenario 1 — Replicator (deploy to AWS, then clone into LocalStack)

deploy-aws:  ## Deploy stack to real AWS (DynamoDB + Lambda + S3)
	cd infra && terraform init -upgrade
	cd infra && terraform apply -auto-approve \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)" \
	    -var="bucket_suffix=$(ACCOUNT_ID)"
	@echo ""
	@echo "AWS API URL:     $$(cd infra && terraform output -raw api_url)"
	@echo "AWS Website URL: $$(cd infra && terraform output -raw website_url_aws)"

seed-aws:  ## Seed AWS DynamoDB with sample products
	TABLE_NAME=$(TABLE_NAME) AWS_REGION=$(AWS_REGION) $(PYTHON) scripts/seed.py aws

test-aws:  ## Smoke-test the AWS API endpoint
	@API=$$(cd infra && terraform output -raw api_url 2>/dev/null || echo ""); \
	[ -n "$$API" ] || { echo "Run 'make deploy-aws' first."; exit 1; }; \
	$(PYTHON) scripts/test.py "$$API"

destroy-aws:  ## Tear down AWS resources
	cd infra && terraform destroy -auto-approve \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)" \
	    -var="bucket_suffix=$(ACCOUNT_ID)"

replicate:  ## Replicate DynamoDB + Lambda from AWS into LocalStack (run after deploy-aws)
	APP_NAME=$(APP_NAME) TABLE_NAME=$(TABLE_NAME) FUNCTION_NAME=$(FUNCTION_NAME) \
	    AWS_REGION=$(AWS_REGION) $(PYTHON) scripts/replicate.py

patch-local-lambda:  ## Push local handler.py to LocalStack without re-deploying to AWS
	cd lambda && zip -q handler.zip handler.py
	awslocal lambda update-function-code \
	    --function-name $(FUNCTION_NAME) \
	    --zip-file fileb://lambda/handler.zip \
	    --query 'LastModified' --output text
	@# Ensure the Lambda uses fake creds so it matches the 000000000000 resource namespace
	awslocal lambda update-function-configuration \
	    --function-name $(FUNCTION_NAME) \
	    --environment "Variables={TABLE_NAME=$(TABLE_NAME),AWS_ACCESS_KEY_ID=test,AWS_SECRET_ACCESS_KEY=test}" \
	    --query 'LastModified' --output text

##@ Scenario 2 — Proxy (run locally, forward DynamoDB calls to real AWS)

deploy-local:  ## Deploy full stack to LocalStack
	cd infra && tflocal init -upgrade
	cd infra && tflocal apply -auto-approve \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)"
	@# LocalStack injects the host's real AWS credentials into Lambda containers, which
	@# puts SDK calls in the real-account namespace (e.g. 411503686428) instead of the
	@# 000000000000 namespace that tflocal/awslocal use.  Pin to fake creds so the Lambda
	@# can find the local DynamoDB table.
	awslocal lambda update-function-configuration \
	    --function-name $(FUNCTION_NAME) \
	    --environment "Variables={TABLE_NAME=$(TABLE_NAME),AWS_ACCESS_KEY_ID=test,AWS_SECRET_ACCESS_KEY=test}" \
	    --query 'LastModified' --output text
	@echo ""
	@echo "Local API URL:     $$(cd infra && tflocal output -raw api_url)"
	@echo "Local Website URL: $$(cd infra && tflocal output -raw website_url_local)"

seed-local:  ## Seed local DynamoDB with sample products
	TABLE_NAME=$(TABLE_NAME) AWS_REGION=$(AWS_REGION) $(PYTHON) scripts/seed.py local

setup-aws-dynamo:  ## Create + seed the DynamoDB table on real AWS (proxy prerequisite)
	@echo "==> Creating table '$(TABLE_NAME)' on AWS..."
	aws dynamodb create-table \
	    --table-name $(TABLE_NAME) \
	    --attribute-definitions AttributeName=id,AttributeType=S \
	    --key-schema AttributeName=id,KeyType=HASH \
	    --billing-mode PAY_PER_REQUEST \
	    --region $(AWS_REGION) 2>/dev/null || echo "  (table already exists)"
	TABLE_NAME=$(TABLE_NAME) AWS_REGION=$(AWS_REGION) $(PYTHON) scripts/seed.py aws

enable-proxy:  ## Forward LocalStack DynamoDB calls to real AWS (open a separate terminal)
	@echo "==> Enabling DynamoDB proxy to real AWS. Press Ctrl+C to stop."
	@echo "    While active, 'make test-local' will return AWS products."
	@echo ""
	localstack aws proxy -c proxy_config.yml

test-local:  ## Smoke-test the local API (works after 'replicate' or 'deploy-local')
	@[ -n "$(LOCAL_API)" ] || { \
	    echo "No local API Gateway found."; \
	    echo "Run 'make replicate' or 'make deploy-local' first."; \
	    exit 1; }
	$(PYTHON) scripts/test.py "$(LOCAL_API)"

destroy-local:  ## Tear down LocalStack resources
	cd infra && tflocal destroy -auto-approve \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)"

##@ Scenario 3 — Cloud Pods (snapshot and restore full LocalStack state)

pod-save:  ## Snapshot current LocalStack state as a Cloud Pod (requires Pro)
	@echo "==> Saving LocalStack state to pod '$(POD_NAME)'..."
	localstack pod save $(POD_NAME)
	@echo "  State saved. Share the pod name to let teammates load it."
	@echo "  Load with: make pod-load  (or: POD_NAME=<name> make pod-load)"

pod-load:  ## Restore a previously saved Cloud Pod into LocalStack (requires Pro)
	@echo "==> Loading pod '$(POD_NAME)' into LocalStack..."
	localstack pod load $(POD_NAME)
	@echo "  State restored. Run 'make test-local' to verify."

pod-list:  ## List all available Cloud Pods
	localstack pod list
