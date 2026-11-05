#!/usr/bin/env bash
# Print the web function URL. LocalStack reports the endpoint domain with :4566;
# swap in the gateway port of the instance we talk to (LOCALSTACK_ENDPOINT).
set -euo pipefail
endpoint="${AWS_ENDPOINT_URL:-${LOCALSTACK_ENDPOINT:-http://localhost.localstack.cloud:4566}}"
domain=$(aws cloudformation describe-stacks --stack-name close-copilot \
  --query "Stacks[0].Outputs[?OutputKey=='WebDomain'].OutputValue" --output text)
echo "http://${domain/:4566/:${endpoint##*:}}"
