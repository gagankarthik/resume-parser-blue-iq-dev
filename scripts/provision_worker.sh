#!/usr/bin/env bash
#
# Provision the async worker stack (SQS queue + DLQ + Worker Lambda + event-source
# mapping) and wire the API function to it. Idempotent: safe to re-run.
#
# WHY THIS EXISTS: Terraform describes this stack but has never been applied to
# production (see docs/DEPLOYMENT.md §6), so `terraform apply` would try to CREATE
# duplicates of every live resource. This script creates only the NEW worker pieces
# against the running account, mirroring infrastructure/terraform/{sqs,lambda,iam}.tf.
#
# PREREQUISITE: run this only AFTER an image containing the SQS worker handler
# (app/handlers/worker_lambda.py) is deployed to the API function — the worker is
# created from the API's current image, so it must already be the new code.
#
# Usage:  CONFIRM=1 AWS_REGION=us-east-2 ./scripts/provision_worker.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
PREFIX="resume-parser-production"
API_FN="${PREFIX}-api"
WORKER_FN="${PREFIX}-worker"
QUEUE="${PREFIX}-worker"
DLQ="${PREFIX}-worker-dlq"
VISIBILITY_TIMEOUT=360      # > worker timeout (300) and the ~130s orchestrator ceiling
MAX_RECEIVE=3               # deliveries before a message goes to the DLQ
WORKER_MEMORY_MB=2048
WORKER_TIMEOUT=300
SQS_BATCH_SIZE=1            # one heavy job per invocation -> elastic scaling

if [[ "${CONFIRM:-}" != "1" ]]; then
  echo "This mutates PRODUCTION ($REGION). Re-run with CONFIRM=1 to proceed." >&2
  exit 1
fi

log() { echo "==> $*"; }

# -- 1. Dead-letter queue ------------------------------------------------------
log "DLQ: $DLQ"
dlq_url=$(aws sqs get-queue-url --queue-name "$DLQ" --region "$REGION" --query QueueUrl --output text 2>/dev/null \
  || aws sqs create-queue --queue-name "$DLQ" --region "$REGION" \
       --attributes MessageRetentionPeriod=1209600 --query QueueUrl --output text)
dlq_arn=$(aws sqs get-queue-attributes --queue-url "$dlq_url" --attribute-names QueueArn \
  --region "$REGION" --query Attributes.QueueArn --output text)

# -- 2. Main queue -------------------------------------------------------------
log "Queue: $QUEUE"
queue_url=$(aws sqs get-queue-url --queue-name "$QUEUE" --region "$REGION" --query QueueUrl --output text 2>/dev/null \
  || aws sqs create-queue --queue-name "$QUEUE" --region "$REGION" --query QueueUrl --output text)
redrive="{\"deadLetterTargetArn\":\"$dlq_arn\",\"maxReceiveCount\":\"$MAX_RECEIVE\"}"
aws sqs set-queue-attributes --queue-url "$queue_url" --region "$REGION" --attributes \
  "VisibilityTimeout=$VISIBILITY_TIMEOUT,ReceiveMessageWaitTimeSeconds=20,MessageRetentionPeriod=86400,RedrivePolicy=$redrive"
queue_arn=$(aws sqs get-queue-attributes --queue-url "$queue_url" --attribute-names QueueArn \
  --region "$REGION" --query Attributes.QueueArn --output text)

# -- 3. IAM: grant the API's execution role SQS produce + consume on the queue --
# The worker reuses the API role (already has DynamoDB/S3/Textract). This inline
# policy adds the queue permissions both functions need.
role_arn=$(aws lambda get-function-configuration --function-name "$API_FN" --region "$REGION" --query Role --output text)
role_name="${role_arn##*/}"
log "IAM: attaching SQS policy to $role_name"
policy=$(cat <<JSON
{ "Version": "2012-10-17", "Statement": [ {
  "Effect": "Allow",
  "Action": ["sqs:SendMessage","sqs:SendMessageBatch","sqs:ReceiveMessage",
             "sqs:DeleteMessage","sqs:GetQueueAttributes","sqs:GetQueueUrl",
             "sqs:ChangeMessageVisibility"],
  "Resource": ["$queue_arn","$dlq_arn"] } ] }
JSON
)
aws iam put-role-policy --role-name "$role_name" --policy-name "${PREFIX}-sqs-access" \
  --policy-document "$policy"

# -- 4. Worker Lambda (same image + role as the API) ---------------------------
image_uri=$(aws lambda get-function --function-name "$API_FN" --region "$REGION" --query Code.ImageUri --output text)
worker_env=$(aws lambda get-function-configuration --function-name "$API_FN" --region "$REGION" \
  --query '{Variables: Environment.Variables}' --output json)

if aws lambda get-function --function-name "$WORKER_FN" --region "$REGION" >/dev/null 2>&1; then
  log "Worker exists: updating image"
  aws lambda update-function-code --function-name "$WORKER_FN" --image-uri "$image_uri" --region "$REGION" >/dev/null
else
  log "Worker: creating $WORKER_FN"
  aws lambda create-function --function-name "$WORKER_FN" --region "$REGION" \
    --package-type Image --code "ImageUri=$image_uri" --role "$role_arn" \
    --timeout "$WORKER_TIMEOUT" --memory-size "$WORKER_MEMORY_MB" \
    --image-config '{"Command":["app.handlers.worker_lambda.handler"]}' \
    --environment "$worker_env" >/dev/null
fi
aws lambda wait function-active-v2 --function-name "$WORKER_FN" --region "$REGION"

# -- 5. Event-source mapping: queue -> worker ----------------------------------
existing_esm=$(aws lambda list-event-source-mappings --function-name "$WORKER_FN" \
  --event-source-arn "$queue_arn" --region "$REGION" --query 'EventSourceMappings[0].UUID' --output text)
if [[ "$existing_esm" == "None" || -z "$existing_esm" ]]; then
  log "Event-source mapping: queue -> worker"
  aws lambda create-event-source-mapping --function-name "$WORKER_FN" \
    --event-source-arn "$queue_arn" --batch-size "$SQS_BATCH_SIZE" \
    --function-response-types ReportBatchItemFailures --region "$REGION" >/dev/null
else
  log "Event-source mapping already present ($existing_esm)"
fi

# -- 6. Point the API at the queue (merge, never replace, the env) -------------
log "API: setting WORKER_QUEUE_URL"
merged_env=$(aws lambda get-function-configuration --function-name "$API_FN" --region "$REGION" \
  --query 'Environment.Variables' --output json \
  | QUEUE_URL="$queue_url" python3 -c \
    'import json,os,sys; v=json.load(sys.stdin); v["WORKER_QUEUE_URL"]=os.environ["QUEUE_URL"]; print(json.dumps({"Variables":v}))')
aws lambda wait function-updated-v2 --function-name "$API_FN" --region "$REGION"
aws lambda update-function-configuration --function-name "$API_FN" --region "$REGION" \
  --environment "$merged_env" >/dev/null

log "Done. Queue: $queue_url"
log "The API now enqueues async jobs; the Worker Lambda drains them."
