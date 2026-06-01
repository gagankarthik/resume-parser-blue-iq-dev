#!/bin/bash
# LocalStack initialization — creates DynamoDB tables and S3 bucket for local dev.
# Runs automatically on LocalStack startup via the init/ready.d hook.

set -e
ENDPOINT="http://localhost:4566"
REGION="us-east-1"

echo "==> Creating S3 bucket..."
awslocal s3 mb s3://resume-parser-temp --region $REGION || true

echo "==> Creating DynamoDB tables..."

# api_keys
awslocal dynamodb create-table \
  --table-name resume-parser-api-keys \
  --attribute-definitions AttributeName=key_hash,AttributeType=S \
  --key-schema AttributeName=key_hash,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION || true

# rate_limits (TTL-based, auto-expires)
awslocal dynamodb create-table \
  --table-name resume-parser-rate-limits \
  --attribute-definitions AttributeName=window_key,AttributeType=S \
  --key-schema AttributeName=window_key,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION || true

awslocal dynamodb update-time-to-live \
  --table-name resume-parser-rate-limits \
  --time-to-live-specification Enabled=true,AttributeName=ttl \
  --region $REGION || true

# jobs (TTL 1 hour)
awslocal dynamodb create-table \
  --table-name resume-parser-jobs \
  --attribute-definitions AttributeName=job_id,AttributeType=S \
  --key-schema AttributeName=job_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION || true

awslocal dynamodb update-time-to-live \
  --table-name resume-parser-jobs \
  --time-to-live-specification Enabled=true,AttributeName=ttl \
  --region $REGION || true

# webhooks (company_id pk + webhook_id sk)
awslocal dynamodb create-table \
  --table-name resume-parser-webhooks \
  --attribute-definitions \
    AttributeName=company_id,AttributeType=S \
    AttributeName=webhook_id,AttributeType=S \
  --key-schema \
    AttributeName=company_id,KeyType=HASH \
    AttributeName=webhook_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION || true

# audit_logs (job_id pk + timestamp sk)
awslocal dynamodb create-table \
  --table-name resume-parser-audit-logs \
  --attribute-definitions \
    AttributeName=job_id,AttributeType=S \
    AttributeName=timestamp,AttributeType=S \
  --key-schema \
    AttributeName=job_id,KeyType=HASH \
    AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION || true

# Seed a development API key: rp_live_devkey00000000000000000000000000000000
# hash of "rp_live_devkey00000000000000000000000000000000"
DEV_KEY_HASH=$(echo -n "rp_live_devkey00000000000000000000000000000000" | sha256sum | awk '{print $1}')

awslocal dynamodb put-item \
  --table-name resume-parser-api-keys \
  --item "{
    \"key_hash\": {\"S\": \"$DEV_KEY_HASH\"},
    \"key_prefix\": {\"S\": \"rp_live_dev…\"},
    \"company_id\": {\"S\": \"dev-company\"},
    \"status\": {\"S\": \"active\"},
    \"rate_limit_per_minute\": {\"N\": \"60\"},
    \"rate_limit_per_day\": {\"N\": \"10000\"},
    \"created_at\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
  }" \
  --region $REGION || true

echo "==> LocalStack init complete."
echo "    Dev API key: rp_live_devkey00000000000000000000000000000000"
