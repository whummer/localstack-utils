AWS_REGION    ?= eu-central-1
export AWS_DEFAULT_REGION = $(AWS_REGION)

APP_NAME      ?= product-catalog
TABLE_NAME     = $(APP_NAME)-products
FUNCTION_NAME  = $(APP_NAME)
POD_NAME      ?= $(APP_NAME)-state
LOCAL_BUCKET   = $(APP_NAME)-frontend-local-$(AWS_REGION)
LOCAL_SUFFIX   = local-$(AWS_REGION)
PYTHON         = python3

# S3 bucket suffix — use your AWS account ID on real AWS to ensure global uniqueness
ACCOUNT_ID    := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "local")

# Resolve the local API Gateway invoke URL without requiring TF state
LOCAL_API     = $(shell awslocal apigatewayv2 get-apis \
                  --region $(AWS_REGION) \
                  --query "Items[?Name=='$(APP_NAME)'].ApiEndpoint" \
                  --output text 2>/dev/null | tr -d '\n')

.DEFAULT_GOAL := help

.PHONY: help \
        localstack-start localstack-stop \
        deploy-aws seed-aws test-aws destroy-aws \
        replicate patch-local-lambda \
        deploy-local seed-local setup-aws-dynamo enable-proxy disable-proxy proxy-status test-local destroy-local \
        pod-save pod-load pod-list

# ── Help ───────────────────────────────────────────────────────────────────────

help:  ## Show available targets
	@awk 'BEGIN {FS = ":.*##"}; \
	     /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }; \
	     /^[a-zA-Z_-]+:.*?## / { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' \
	$(MAKEFILE_LIST)
	@echo ""

##@ LocalStack

localstack-start:  ## Start LocalStack (Pro required for proxy + Cloud Pods)
	EXTRA_CORS_ALLOWED_ORIGINS=https://aws-replicator.localhost.localstack.cloud:4566 \
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
	    -state=terraform.aws.tfstate \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)" \
	    -var="bucket_suffix=$(ACCOUNT_ID)-$(AWS_REGION)"
	@echo ""
	@echo "AWS API URL:     $$(cd infra && terraform output -state=terraform.aws.tfstate -raw api_url)"
	@echo "AWS Website URL: $$(cd infra && terraform output -state=terraform.aws.tfstate -raw website_url_aws)"

seed-aws:  ## Seed AWS DynamoDB with sample products
	TABLE_NAME=$(TABLE_NAME) AWS_REGION=$(AWS_REGION) $(PYTHON) scripts/seed.py aws

test-aws:  ## Smoke-test the AWS API endpoint
	@API=$$(cd infra && terraform output -state=terraform.aws.tfstate -raw api_url 2>/dev/null || echo ""); \
	[ -n "$$API" ] || { echo "Run 'make deploy-aws' first."; exit 1; }; \
	$(PYTHON) scripts/test.py "$$API"

destroy-aws:  ## Tear down AWS resources
	cd infra && terraform destroy -auto-approve \
	    -state=terraform.aws.tfstate \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)" \
	    -var="bucket_suffix=$(ACCOUNT_ID)-$(AWS_REGION)"

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
	    -state=terraform.local.tfstate \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)" \
	    -var="bucket_suffix=$(LOCAL_SUFFIX)"
	@# LocalStack injects the host's real AWS credentials into Lambda containers, which
	@# puts SDK calls in the real-account namespace (e.g. 411503686428) instead of the
	@# 000000000000 namespace that tflocal/awslocal use.  Pin to fake creds so the Lambda
	@# can find the local DynamoDB table.
	awslocal lambda update-function-configuration \
	    --function-name $(FUNCTION_NAME) \
	    --environment "Variables={TABLE_NAME=$(TABLE_NAME),AWS_ACCESS_KEY_ID=test,AWS_SECRET_ACCESS_KEY=test}" \
	    --query 'LastModified' --output text
	@# tflocal stores an AWS-format URL in state for invoke_url; overwrite config.json
	@# with the actual local endpoint so the frontend auto-populates the correct URL.
	@printf '{"apiUrl":"%s"}' "$(LOCAL_API)" | \
	awslocal s3 cp - "s3://$(LOCAL_BUCKET)/config.json" \
	    --content-type application/json
	@echo ""
	@echo "Local API URL:     $(LOCAL_API)"
	@echo "Local Website URL: $$(cd infra && tflocal output -state=terraform.local.tfstate -raw website_url_local)"

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

enable-proxy:  ## Forward LocalStack DynamoDB calls to real AWS via the proxy extension
	@echo "==> Enabling DynamoDB proxy to real AWS..."
	@curl -sf -X POST http://localhost:4566/_localstack/aws/proxies \
	  -H 'Content-Type: application/json' \
	  -d "{\"config\":{\"services\":{\"dynamodb\":{\"resources\":[\".*\"]}}},\"env_vars\":{\"AWS_ACCESS_KEY_ID\":\"$$AWS_ACCESS_KEY_ID\",\"AWS_SECRET_ACCESS_KEY\":\"$$AWS_SECRET_ACCESS_KEY\",\"AWS_SESSION_TOKEN\":\"$$AWS_SESSION_TOKEN\"}}"
	@echo ""
	@echo "  DynamoDB proxy enabled. Run 'make test-local' to see AWS data."
	@echo "  Run 'make disable-proxy' to stop forwarding."

disable-proxy:  ## Stop forwarding LocalStack DynamoDB calls to real AWS
	@echo "==> Disabling DynamoDB proxy..."
	@curl -sf -X POST http://localhost:4566/_localstack/aws/proxies/status \
	  -H 'Content-Type: application/json' \
	  -d '{"status": "disabled"}'
	@echo ""
	@echo "  DynamoDB proxy disabled. Calls now go to local DynamoDB."

proxy-status:  ## Show current proxy status
	curl -s http://localhost:4566/_localstack/aws/proxies/status | python3 -m json.tool

test-local:  ## Smoke-test the local API (works after 'replicate' or 'deploy-local')
	@[ -n "$(LOCAL_API)" ] || { \
	    echo "No local API Gateway found."; \
	    echo "Run 'make replicate' or 'make deploy-local' first."; \
	    exit 1; }
	$(PYTHON) scripts/test.py "$(LOCAL_API)"

destroy-local:  ## Tear down LocalStack resources
	cd infra && tflocal destroy -auto-approve \
	    -state=terraform.local.tfstate \
	    -var="aws_region=$(AWS_REGION)" \
	    -var="app_name=$(APP_NAME)" \
	    -var="bucket_suffix=$(LOCAL_SUFFIX)"

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
