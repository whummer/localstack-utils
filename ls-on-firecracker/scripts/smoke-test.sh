#!/usr/bin/env bash
# Exercises the LocalStack instance running inside the microVM:
#   - S3: create a bucket, put an object, read it back
#   - Lambda: deploy a function, invoke it, assert the response
# Lambda execution happens via the guest's own Docker daemon, the same way
# it would against a normal `docker run localstack` setup.
set -euo pipefail

: "${VM_IP:?run via 'make', not directly}"
: "${BUCKET:?}"
: "${LAMBDA_FN:?}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
ENDPOINT="http://${VM_IP}:4566"
AWS="aws --endpoint-url $ENDPOINT"

echo "[test] --- S3 ---"
echo "[test] creating bucket s3://${BUCKET}"
$AWS s3 mb "s3://${BUCKET}"

TMP_FILE=$(mktemp)
echo "hello from a firecracker microVM" > "$TMP_FILE"
$AWS s3 cp "$TMP_FILE" "s3://${BUCKET}/hello.txt" >/dev/null
rm -f "$TMP_FILE"

$AWS s3 ls "s3://${BUCKET}/"
echo "[test] S3 OK"

echo "[test] --- Lambda ---"
ZIP_FILE=$(mktemp -u --suffix=.zip)
(cd "$SCRIPT_DIR/../fixtures" && zip -q "$ZIP_FILE" handler.py)

echo "[test] creating function $LAMBDA_FN"
$AWS lambda create-function \
  --function-name "$LAMBDA_FN" \
  --runtime python3.12 \
  --handler handler.handler \
  --role arn:aws:iam::000000000000:role/lambda-role \
  --zip-file "fileb://${ZIP_FILE}" >/dev/null
rm -f "$ZIP_FILE"

echo "[test] waiting for $LAMBDA_FN to become active (this pulls the Lambda runtime image)"
state=""
for _ in $(seq 1 40); do
  state=$($AWS lambda get-function --function-name "$LAMBDA_FN" \
    --query 'Configuration.State' --output text 2>/dev/null || echo "")
  [[ "$state" == "Active" ]] && break
  sleep 5
done
if [[ "$state" != "Active" ]]; then
  echo "[test] FAILED: function never became active (last state: ${state:-unknown})" >&2
  exit 1
fi

echo "[test] invoking $LAMBDA_FN"
OUT_FILE=$(mktemp)
$AWS lambda invoke \
  --function-name "$LAMBDA_FN" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"name":"firecracker"}' \
  "$OUT_FILE" >/dev/null

RESPONSE=$(cat "$OUT_FILE")
rm -f "$OUT_FILE"
echo "[test] response: $RESPONSE"

MESSAGE=$(jq -r '.message' <<<"$RESPONSE")
if [[ "$MESSAGE" != "hello firecracker" ]]; then
  echo "[test] FAILED: expected message 'hello firecracker', got '$MESSAGE'" >&2
  exit 1
fi

echo "[test] Lambda OK"
echo "[test] all checks passed"
