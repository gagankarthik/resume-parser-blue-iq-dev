# -- Shared environment config -------------------------------------------------

locals {
  lambda_env = {
    ENVIRONMENT               = var.environment
    AWS_REGION_NAME           = var.aws_region # avoid conflict with Lambda's AWS_REGION
    DYNAMODB_TABLE_API_KEYS   = aws_dynamodb_table.api_keys.name
    DYNAMODB_TABLE_JOBS       = aws_dynamodb_table.jobs.name
    DYNAMODB_TABLE_BATCHES    = aws_dynamodb_table.batches.name
    DYNAMODB_TABLE_WEBHOOKS   = aws_dynamodb_table.webhooks.name
    DYNAMODB_TABLE_AUDIT_LOGS = aws_dynamodb_table.audit_logs.name
    DYNAMODB_TABLE_COMPANIES  = aws_dynamodb_table.companies.name
    DYNAMODB_TABLE_FEEDBACK   = aws_dynamodb_table.feedback.name
    S3_BUCKET_NAME            = aws_s3_bucket.temp.bucket
    OPENAI_MODEL              = var.openai_model
    OPENAI_MAX_TOKENS         = "4096"
    MAX_FILE_SIZE_MB          = "10" # NOTE: Function URL still caps requests at ~6 MB at the edge
    MAX_BATCH_SIZE            = tostring(var.max_batch_size)
    MAX_CONCURRENT_JOBS       = "5" # local-dev batch semaphore only
    JOB_RESULT_TTL_SECONDS    = "3600"
    # The API enqueues async OCR work here; the Worker Lambda drains it. Empty on
    # the worker itself is harmless (it consumes via the event-source mapping).
    WORKER_QUEUE_URL = aws_sqs_queue.worker.url
    OPENAI_API_KEY   = var.openai_api_key
    ADMIN_API_TOKEN  = var.admin_api_token
    AUTH_SECRET      = var.auth_secret
    # GigHealth Partner API key - enables the live cities lookup at parse time.
    # Facility/geography/specialty ids resolve offline from bundled snapshots and
    # do NOT need this; when empty, city mapping is a safe no-op.
    GIG_SPECIALTIES_API_KEY = var.gig_specialties_api_key
  }
}

# -- CloudWatch Log Groups -----------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name_prefix}-api"
  retention_in_days = 30
  tags              = local.common_tags
}

# -- API Lambda (HTTP API; enqueues async work onto SQS) -----------------------
# Serves Function URL events via FastAPI and pushes async parse jobs onto the
# worker queue. The heavy OCR/multi-agent path runs on the separate Worker Lambda
# below, so this function is sized for request handling.

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

  lifecycle {
    # CI owns the running image: the deploy workflow builds a SHA-tagged image and
    # updates the function via `aws lambda update-function-code`. `var.ecr_image_uri`
    # here only seeds the FIRST create (and pins a valid image if the function is
    # ever recreated). Ignoring image_uri stops Terraform from reverting CI's
    # SHA-tagged image back to `:latest` on every apply - so `terraform apply` only
    # touches env/config, never the code.
    ignore_changes = [image_uri]
  }

  tags = local.common_tags
}

# Public HTTPS endpoint - no API Gateway overhead.
# CORS is intentionally NOT configured here; the FastAPI app owns CORS so the
# response carries exactly one set of Access-Control-* headers.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE" # auth is handled by our API key middleware
}

# Public-invoke permission for the API Function URL.
# Without this, an AuthType=NONE URL still returns HTTP 403 for unsigned
# requests - the resource-based policy must explicitly allow lambda:InvokeFunctionUrl.
resource "aws_lambda_permission" "api_url_public" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# -- Worker Lambda (async OCR / multi-agent pipeline) --------------------------
# Same container image, different entry point (app.handlers.worker_lambda.handler).
# A dedicated function so the heavy path's memory + timeout + concurrency are sized
# for OCR/Textract and the 10-way per-role fan-out WITHOUT taxing the API function's
# cost or cold-start profile. Fed by the SQS event-source mapping (sqs.tf).

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name_prefix}-worker"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name_prefix}-worker"
  role          = aws_iam_role.worker_exec.arn
  package_type  = "Image"
  image_uri     = var.ecr_image_uri
  timeout       = var.worker_lambda_timeout_seconds
  memory_size   = var.worker_lambda_memory_mb

  # -1 (Terraform's "unset") leaves the function on the account's unreserved pool;
  # set a positive value to cap the worker's fan-out during a batch burst.
  reserved_concurrent_executions = var.worker_reserved_concurrency

  image_config {
    command = ["app.handlers.worker_lambda.handler"]
  }

  environment {
    variables = local.lambda_env
  }

  depends_on = [
    aws_cloudwatch_log_group.worker,
    aws_iam_role_policy.worker_app,
  ]

  lifecycle {
    # CI owns the running image for BOTH functions (same SHA-tagged image). See the
    # API function above - ignore image_uri so `terraform apply` only touches config.
    ignore_changes = [image_uri]
  }

  tags = local.common_tags
}

