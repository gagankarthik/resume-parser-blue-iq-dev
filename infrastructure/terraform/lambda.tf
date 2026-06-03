# ── Shared environment config ─────────────────────────────────────────────────

locals {
  lambda_env = {
    ENVIRONMENT                   = var.environment
    AWS_REGION_NAME               = var.aws_region # avoid conflict with Lambda's AWS_REGION
    DYNAMODB_TABLE_API_KEYS       = aws_dynamodb_table.api_keys.name
    DYNAMODB_TABLE_RATE_LIMITS    = aws_dynamodb_table.rate_limits.name
    DYNAMODB_TABLE_JOBS           = aws_dynamodb_table.jobs.name
    DYNAMODB_TABLE_BATCHES        = aws_dynamodb_table.batches.name
    DYNAMODB_TABLE_WEBHOOKS       = aws_dynamodb_table.webhooks.name
    DYNAMODB_TABLE_AUDIT_LOGS     = aws_dynamodb_table.audit_logs.name
    S3_BUCKET_NAME                = aws_s3_bucket.temp.bucket
    OPENAI_MODEL                  = var.openai_model
    OPENAI_MAX_TOKENS             = "4096"
    MAX_FILE_SIZE_MB              = "10" # NOTE: Function URL still caps requests at ~6 MB at the edge
    MAX_BATCH_SIZE                = tostring(var.max_batch_size)
    MAX_CONCURRENT_JOBS           = "5" # local-dev batch semaphore only
    DEFAULT_RATE_LIMIT_PER_MINUTE = tostring(var.rate_limit_per_minute)
    DEFAULT_RATE_LIMIT_PER_DAY    = tostring(var.rate_limit_per_day)
    JOB_RESULT_TTL_SECONDS        = "3600"
    # Single-function deployment: the function invokes ITSELF for async OCR work.
    WORKER_LAMBDA_FUNCTION_NAME = "${local.name_prefix}-api"
    OPENAI_API_KEY              = var.openai_api_key
  }
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name_prefix}-api"
  retention_in_days = 30
  tags              = local.common_tags
}

# ── Resume-parser Lambda (single function: HTTP API + async OCR worker) ─────────
# The unified handler routes Function URL events to FastAPI and self-invoked
# events to the OCR pipeline. Sized for the heavier OCR path; HTTP requests
# simply use less of the budget.

resource "aws_lambda_function" "api" {
  function_name = "${local.name_prefix}-api"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = var.ecr_image_uri
  timeout       = var.api_lambda_timeout_seconds
  memory_size   = var.api_lambda_memory_mb

  image_config {
    command = ["app.handlers.lambda_handler.handler"]
  }

  environment {
    variables = local.lambda_env
  }

  depends_on = [
    aws_cloudwatch_log_group.api,
    aws_iam_role_policy.lambda_app,
  ]

  tags = local.common_tags
}

# Public HTTPS endpoint — no API Gateway overhead.
# CORS is intentionally NOT configured here; the FastAPI app owns CORS so the
# response carries exactly one set of Access-Control-* headers.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE" # auth is handled by our API key middleware
}

# Public-invoke permission for the API Function URL.
# Without this, an AuthType=NONE URL still returns HTTP 403 for unsigned
# requests — the resource-based policy must explicitly allow lambda:InvokeFunctionUrl.
resource "aws_lambda_permission" "api_url_public" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

