# ── Shared environment config ─────────────────────────────────────────────────

locals {
  lambda_env = {
    ENVIRONMENT                    = var.environment
    AWS_REGION_NAME                = var.aws_region   # avoid conflict with Lambda's AWS_REGION
    DYNAMODB_TABLE_API_KEYS        = aws_dynamodb_table.api_keys.name
    DYNAMODB_TABLE_RATE_LIMITS     = aws_dynamodb_table.rate_limits.name
    DYNAMODB_TABLE_JOBS            = aws_dynamodb_table.jobs.name
    DYNAMODB_TABLE_BATCHES         = aws_dynamodb_table.batches.name
    DYNAMODB_TABLE_WEBHOOKS        = aws_dynamodb_table.webhooks.name
    DYNAMODB_TABLE_AUDIT_LOGS      = aws_dynamodb_table.audit_logs.name
    S3_BUCKET_NAME                 = aws_s3_bucket.temp.bucket
    OPENAI_MODEL                   = var.openai_model
    OPENAI_MAX_TOKENS              = "4096"
    MAX_FILE_SIZE_MB               = "10"
    MAX_BATCH_SIZE                 = tostring(var.max_batch_size)
    MAX_CONCURRENT_JOBS            = tostring(var.worker_reserved_concurrency)
    DEFAULT_RATE_LIMIT_PER_MINUTE  = tostring(var.rate_limit_per_minute)
    DEFAULT_RATE_LIMIT_PER_DAY     = tostring(var.rate_limit_per_day)
    JOB_RESULT_TTL_SECONDS         = "3600"
    WORKER_LAMBDA_FUNCTION_NAME    = "${local.name_prefix}-worker"
    # OpenAI key is loaded from SSM at runtime — not an env var
  }
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name_prefix}-api"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name_prefix}-worker"
  retention_in_days = 30
  tags              = local.common_tags
}

# ── API Lambda ────────────────────────────────────────────────────────────────

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

# API Lambda URL (public HTTPS endpoint — no API Gateway overhead)
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE"   # auth is handled by our API key middleware

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "DELETE"]
    allow_headers = ["X-API-Key", "X-Request-ID", "Content-Type"]
    expose_headers = ["X-Request-ID", "X-RateLimit-Limit-Minute",
                      "X-RateLimit-Remaining-Minute", "X-RateLimit-Limit-Day",
                      "X-RateLimit-Remaining-Day"]
    max_age = 300
  }
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

# ── Worker Lambda ─────────────────────────────────────────────────────────────

resource "aws_lambda_function" "worker" {
  function_name = "${local.name_prefix}-worker"
  role          = aws_iam_role.lambda_exec.arn
  package_type  = "Image"
  image_uri     = var.ecr_image_uri
  timeout       = var.worker_lambda_timeout_seconds
  memory_size   = var.worker_lambda_memory_mb

  # Reserved concurrency = max parallel OCR+AI calls
  # This prevents thundering herd against OpenAI and Textract
  reserved_concurrent_executions = var.worker_reserved_concurrency

  image_config {
    command = ["app.handlers.worker_lambda.handler"]
  }

  environment {
    variables = local.lambda_env
  }

  depends_on = [
    aws_cloudwatch_log_group.worker,
    aws_iam_role_policy.lambda_app,
  ]

  tags = local.common_tags
}
