variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-2"
}

variable "environment" {
  description = "Deployment environment (production | staging)"
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "staging"], var.environment)
    error_message = "environment must be production or staging"
  }
}

variable "openai_api_key" {
  description = "OpenAI API key - passed to the Lambda as an env var. Provide via TF_VAR_openai_api_key (never commit it). Marked sensitive so it is redacted from CLI output, but note it is still present in Terraform state - keep the state backend (S3) encrypted and access-restricted."
  type        = string
  sensitive   = true
}

variable "openai_model" {
  description = "OpenAI model ID to use for parsing (e.g. gpt-4.1-mini, gpt-4o)"
  type        = string
  default     = "gpt-4.1-mini"
}

variable "gig_specialties_api_key" {
  description = "GigHealth Partner API key (x-api-key) - passed to the Lambda as GIG_SPECIALTIES_API_KEY. Enables the live cities lookup at parse time; the facility/geography/specialty snapshots resolve offline and do NOT need it. Provide via TF_VAR_gig_specialties_api_key or the local terraform.tfvars (never commit it). Marked sensitive so it is redacted from CLI output; note it is still present in Terraform state - keep the state backend encrypted and access-restricted. Empty leaves city mapping a safe no-op."
  type        = string
  sensitive   = true
  default     = ""
}

variable "admin_api_token" {
  description = "Bearer token gating the /api/v1/admin/* endpoints (product platform)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "auth_secret" {
  description = "HMAC secret for signing self-serve account session tokens. Required - generate with: openssl rand -hex 32"
  type        = string
  sensitive   = true

  validation {
    # Block deploys that ship the insecure in-code development default.
    condition     = length(var.auth_secret) >= 32 && var.auth_secret != "dev-insecure-auth-secret-change-me"
    error_message = "auth_secret must be a strong value (>= 32 chars) and not the development default. Generate one with: openssl rand -hex 32"
  }
}

variable "ecr_image_uri" {
  description = "Full ECR image URI including tag (e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/resume-parser-lambda:abc1234)"
  type        = string
}

# Single function handles both the HTTP API and the async OCR path, so it is
# sized for the heavier OCR workload (Tesseract + Textract + OpenAI).
variable "api_lambda_memory_mb" {
  description = "Memory for the resume-parser Lambda (MB) - sized for OCR"
  type        = number
  default     = 2048
}

variable "api_lambda_timeout_seconds" {
  description = "Lambda timeout (s) - covers async OCR self-invocations; HTTP requests finish well under this"
  type        = number
  default     = 300
}

variable "max_batch_size" {
  description = "Maximum files accepted per batch request"
  type        = number
  default     = 200
}

# -- Worker Lambda (async OCR / multi-agent, fed by SQS) -----------------------

variable "worker_lambda_memory_mb" {
  description = "Memory for the Worker Lambda (MB) - sized for OCR + the 10-way per-role LLM fan-out"
  type        = number
  default     = 2048
}

variable "worker_lambda_timeout_seconds" {
  description = "Worker Lambda timeout (s). Must be < worker_queue_visibility_timeout_seconds so a running job is never redelivered."
  type        = number
  default     = 300
}

variable "worker_reserved_concurrency" {
  description = "Reserved concurrency for the Worker Lambda. -1 leaves it on the account's unreserved pool; set a positive value to cap fan-out during batch bursts."
  type        = number
  default     = -1
}

variable "worker_sqs_batch_size" {
  description = "Messages the SQS event-source mapping hands the Worker per invocation. 1 = one heavy job per invocation (elastic scaling); raise only for light workloads."
  type        = number
  default     = 1
}

variable "worker_event_source_max_concurrency" {
  description = "Max concurrent Worker invocations the event-source mapping will drive (SQS backpressure lever, protects the OpenAI TPM budget). 0 = unbounded (up to account concurrency); a set value must be between 2 and 1000."
  type        = number
  default     = 0

  validation {
    condition     = var.worker_event_source_max_concurrency == 0 || (var.worker_event_source_max_concurrency >= 2 && var.worker_event_source_max_concurrency <= 1000)
    error_message = "worker_event_source_max_concurrency must be 0 (unbounded) or between 2 and 1000."
  }
}

# -- Worker queue tuning -------------------------------------------------------

variable "worker_queue_visibility_timeout_seconds" {
  description = "How long a claimed message is hidden from other consumers. Must exceed worker_lambda_timeout_seconds (and the ~130s orchestrator ceiling) so a still-running job is not picked up twice."
  type        = number
  default     = 360
}

variable "worker_queue_max_receive_count" {
  description = "Deliveries attempted before a message is routed to the DLQ as a poison pill."
  type        = number
  default     = 3
}

# -- Alarms --------------------------------------------------------------------

variable "alarm_sns_topic_arn" {
  description = "SNS topic ARN the CloudWatch alarms notify. Empty = alarms still evaluate and show in the console but send no notification."
  type        = string
  default     = ""
}

variable "worker_queue_backlog_alarm_threshold" {
  description = "Alarm when visible messages on the worker queue stay above this for 5 minutes (backpressure)."
  type        = number
  default     = 100
}

variable "worker_errors_alarm_threshold" {
  description = "Alarm when the Worker Lambda logs more than this many errors in a 5-minute window."
  type        = number
  default     = 5
}

# Custom API domain (optional). Leave empty to keep only the raw Lambda Function
# URL. Set to e.g. "api.parsinglab.blue-iq.ai" to front the Function URL with a
# CloudFront distribution + ACM cert on that hostname (see cloudfront_api.tf).
# DNS for the domain is external (GoDaddy), so after `terraform apply` you add the
# ACM validation CNAME and the final CNAME (-> cloudfront_domain_name output) there.
variable "api_custom_domain" {
  description = "Custom hostname for the API (e.g. api.parsinglab.blue-iq.ai). Empty = disabled."
  type        = string
  default     = ""
}
