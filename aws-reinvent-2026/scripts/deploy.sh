#!/usr/bin/env bash
# Deploy the stack and seed the demo data. Target endpoint is configurable:
#   LOCALSTACK_ENDPOINT=http://localhost.localstack.cloud:4566 (default)
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_ENDPOINT_URL="${LOCALSTACK_ENDPOINT:-http://localhost.localstack.cloud:4566}"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
ARCH="${ARCH:-$(uname -m | sed 's/x86_64/x86_64/;s/aarch64/arm64/;s/arm64/arm64/')}"

./scripts/build.sh

# The agent's brain runs as a Bedrock AgentCore Runtime (LocalStack: ENABLE_BEDROCK_AGENTCORE=1).
export MODEL_MODE="${MODEL_MODE:-agentcore}"
AGENT_ARN=""
if [ "$MODEL_MODE" = agentcore ]; then AGENT_ARN=$(python3 scripts/deploy_agent.py); fi
aws s3 mb s3://close-code >/dev/null 2>&1 || true
# Lambda Web Functions load their code from a versioning-enabled bucket.
aws s3api put-bucket-versioning --bucket close-code --versioning-configuration Status=Enabled
# Content-hashed keys so CloudFormation notices code changes.
key() { echo "$1-$(shasum -a 256 build/$1.zip | cut -c1-12).zip"; }
for fn in workflow web notifier sandbox; do aws s3 cp --quiet build/$fn.zip s3://close-code/$(key $fn); done

aws cloudformation deploy --stack-name close-copilot --template-file template.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides CodeBucket=close-code WorkflowKey=$(key workflow) \
  WebKey=$(key web) NotifierKey=$(key notifier) SandboxKey=$(key sandbox) Architecture="$ARCH" ModelMode="$MODEL_MODE" AgentRuntimeArn="$AGENT_ARN" AgentMode="${AGENT_MODEL_MODE:-rules}"

python3 scripts/seed.py

aws lambda wait function-active-v2 --function-name close-workflow
echo; echo "Finance Closing Copilot is up: $(./scripts/url.sh)"
