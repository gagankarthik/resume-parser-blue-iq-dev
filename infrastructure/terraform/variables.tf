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

variable "ecr_image_uri" {
  description = "Full ECR image URI including tag (e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/resume-parser-lambda:abc1234)"
  type        = string
}

variable "api_lambda_memory_mb" {
  description = "Memory for the API Lambda (MB)"
  type        = number
  default     = 1024
}

variable "worker_lambda_memory_mb" {
  description = "Memory for the Worker Lambda (scanned PDFs need more RAM for Tesseract)"
  type        = number
  default     = 2048
}

variable "api_lambda_timeout_seconds" {
  description = "API Lambda timeout — API Gateway hard-limits at 29s"
  type        = number
  default     = 29
}

variable "worker_lambda_timeout_seconds" {
  description = "Worker Lambda timeout — Tesseract + Textract + OpenAI"
  type        = number
  default     = 300
}

variable "worker_reserved_concurrency" {
  description = "Reserved concurrency for worker Lambda — controls max parallel OCR+AI calls"
  type        = number
  default     = 10
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
