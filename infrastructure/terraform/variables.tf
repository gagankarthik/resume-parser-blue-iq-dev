variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
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
  description = "OpenAI API key — stored in SSM Parameter Store, never in state"
  type        = string
  sensitive   = true
}

variable "openai_model" {
  description = "OpenAI model ID to use for parsing (e.g. gpt-4o, gpt-4.1-mini)"
  type        = string
  default     = "gpt-4o"
}

variable "admin_api_token" {
  description = "Bearer token gating the /api/v1/admin/* endpoints (product platform)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "ecr_image_uri" {
  description = "Full ECR image URI including tag (e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/resume-parser-lambda:abc1234)"
  type        = string
}

# Single function handles both the HTTP API and the async OCR path, so it is
# sized for the heavier OCR workload (Tesseract + Textract + OpenAI).
variable "api_lambda_memory_mb" {
  description = "Memory for the resume-parser Lambda (MB) — sized for OCR"
  type        = number
  default     = 2048
}

variable "api_lambda_timeout_seconds" {
  description = "Lambda timeout (s) — covers async OCR self-invocations; HTTP requests finish well under this"
  type        = number
  default     = 300
}

variable "max_batch_size" {
  description = "Maximum files accepted per batch request"
  type        = number
  default     = 200
}

variable "rate_limit_per_minute" {
  description = "Default per-key per-minute rate limit"
  type        = number
  default     = 30
}

variable "rate_limit_per_day" {
  description = "Default per-key per-day rate limit"
  type        = number
  default     = 1000
}
