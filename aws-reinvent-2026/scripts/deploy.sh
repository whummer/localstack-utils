#!/usr/bin/env bash
# Deploy the stack and seed the stock table. Target endpoint is configurable:
#   LOCALSTACK_ENDPOINT=http://localhost.localstack.cloud:4566 (default)
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_ENDPOINT_URL="${LOCALSTACK_ENDPOINT:-http://localhost.localstack.cloud:4566}"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
ARCH="${ARCH:-$(uname -m | sed 's/x86_64/x86_64/;s/aarch64/arm64/;s/arm64/arm64/')}"

./scripts/build.sh
aws s3 mb s3://procure-code >/dev/null 2>&1 || true
# Content-hashed keys so CloudFormation notices code changes.
key() { echo "$1-$(shasum -a 256 build/$1.zip | cut -c1-12).zip"; }
for fn in workflow web notifier; do aws s3 cp --quiet build/$fn.zip s3://procure-code/$(key $fn); done

aws cloudformation deploy --stack-name procurement --template-file template.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides CodeBucket=procure-code WorkflowKey=$(key workflow) \
  WebKey=$(key web) NotifierKey=$(key notifier) Architecture="$ARCH" ModelMode="${MODEL_MODE:-rules}"

vendor() { aws dynamodb put-item --table-name procure-vendors --item \
  "{\"vendorId\":{\"S\":\"$1\"},\"name\":{\"S\":\"$2\"},\"approved\":{\"BOOL\":$3},\"risk\":{\"N\":\"$4\"}}"; }
budget() { aws dynamodb put-item --table-name procure-budgets --item \
  "{\"department\":{\"S\":\"$1\"},\"remaining_cents\":{\"N\":\"$2\"},\"total_cents\":{\"N\":\"$2\"}}"; }
vendor v1 "Dell Technologies" true 12
vendor v2 "Cisco Systems" true 18
vendor v3 "Acme Office Supplies" true 8
vendor v4 "ShadyCorp" false 91
budget engineering 10000000
budget it 6000000
budget marketing 2000000
budget hr 1500000

aws lambda wait function-active-v2 --function-name procure-workflow
echo; echo "Procurement Copilot is up: $(aws cloudformation describe-stacks --stack-name procurement --query 'Stacks[0].Outputs[0].OutputValue' --output text)"
