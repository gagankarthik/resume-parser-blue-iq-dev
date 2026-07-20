#!/usr/bin/env bash
#
# Set ONE environment variable on a Lambda function without wiping the others.
# Use this to rotate a secret (OpenAI key, admin token, GigHealth key, AUTH_SECRET)
# and re-set it on the function.
#
# The value is read from the SECRET_VALUE env var - never passed as a CLI argument -
# so it does not land in shell history or a `ps` process listing, and it is never
# echoed. The function's other variables are preserved (read -> merge -> write).
#
# Usage:
#   SECRET_VALUE='<new-secret>' ./scripts/set_lambda_secret.sh <function-name> <VAR_NAME>
#
# Examples:
#   SECRET_VALUE='sk-proj-...'  ./scripts/set_lambda_secret.sh resume-parser-production-api OPENAI_API_KEY
#   SECRET_VALUE="$(openssl rand -hex 32)" ./scripts/set_lambda_secret.sh resume-parser-production-api AUTH_SECRET
set -euo pipefail

FN="${1:?function name required (e.g. resume-parser-production-api)}"
VAR="${2:?variable name required (e.g. OPENAI_API_KEY)}"
REGION="${AWS_REGION:-us-east-2}"
: "${SECRET_VALUE:?Set SECRET_VALUE in the environment, not as an argument}"

# Wait out any in-flight update so the write is not rejected, then merge the new
# value into the existing variable map and write it back.
aws lambda wait function-updated-v2 --function-name "$FN" --region "$REGION" 2>/dev/null || true

merged=$(aws lambda get-function-configuration --function-name "$FN" --region "$REGION" \
  --query 'Environment.Variables' --output json \
  | VAR="$VAR" python3 -c \
    'import json,os,sys; v=json.load(sys.stdin); v[os.environ["VAR"]]=os.environ["SECRET_VALUE"]; print(json.dumps({"Variables": v}))')

aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
  --environment "$merged" >/dev/null

echo "Set $VAR on $FN in $REGION (value not printed). Other variables preserved."
