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
  description = "OpenAI API key — passed to the Lambda as an env var. Provide via TF_VAR_openai_api_key (never commit it). Marked sensitive so it is redacted from CLI output, but note it is still present in Terraform state — keep the state backend (S3) encrypted and access-restricted."
  type        = string
  sensitive   = true
}

variable "openai_model" {
  description = "OpenAI model ID to use for parsing (e.g. gpt-4.1-mini, gpt-4o)"
  type        = string
  default     = "gpt-4.1-mini"
}

variable "admin_api_token" {
  description = "Bearer token gating the /api/v1/admin/* endpoints (product platform)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "auth_secret" {
  description = "HMAC secret for signing self-serve account session tokens. Required — generate with: openssl rand -hex 32"
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

# Custom API domain (optional). Leave empty to keep only the raw Lambda Function
# URL. Set to e.g. "api.parsinglab.blue-iq.ai" to front the Function URL with a
# CloudFront distribution + ACM cert on that hostname (see cloudfront_api.tf).
# DNS for the domain is external (GoDaddy), so after `terraform apply` you add the
# ACM validation CNAME and the final CNAME (→ cloudfront_domain_name output) there.
variable "api_custom_domain" {
  description = "Custom hostname for the API (e.g. api.parsinglab.blue-iq.ai). Empty = disabled."
  type        = string
  default     = ""
}
