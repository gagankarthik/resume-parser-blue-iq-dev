# -- DynamoDB Tables -----------------------------------------------------------
# All tables use PAY_PER_REQUEST (on-demand) - no capacity planning needed.
# TTL is enabled where applicable for automatic cleanup.

resource "aws_dynamodb_table" "api_keys" {
  name         = "resume-parser-api-keys"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key_hash"

  attribute {
    name = "key_hash"
    type = "S"
  }

  attribute {
    name = "company_id"
    type = "S"
  }

  # List all keys belonging to a company (admin dashboard).
  global_secondary_index {
    name            = "company-index"
    hash_key        = "company_id"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}

resource "aws_dynamodb_table" "jobs" {
  name         = "resume-parser-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}

resource "aws_dynamodb_table" "batches" {
  name         = "resume-parser-batches"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "batch_id"

  attribute {
    name = "batch_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = local.common_tags
}

resource "aws_dynamodb_table" "webhooks" {
  name         = "resume-parser-webhooks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "company_id"
  range_key    = "webhook_id"

  attribute {
    name = "company_id"
    type = "S"
  }

  attribute {
    name = "webhook_id"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}

resource "aws_dynamodb_table" "audit_logs" {
  name         = "resume-parser-audit-logs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"
  range_key    = "timestamp"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  attribute {
    name = "company_id"
    type = "S"
  }

  # Usage/stats queries: fetch a company's audit records over a time range
  # (sum ai_tokens_used, count by status, etc.) without scanning the table.
  global_secondary_index {
    name            = "company-timestamp-index"
    hash_key        = "company_id"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  # Audit logs retained 90 days
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}

# Company / account records - powers onboarding and the product dashboard.
# company_id is the same identifier used on api_keys and audit_logs.
resource "aws_dynamodb_table" "companies" {
  name         = "resume-parser-companies"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "company_id"

  attribute {
    name = "company_id"
    type = "S"
  }

  attribute {
    name = "email"
    type = "S"
  }

  # Look up an account by email during onboarding / sign-in.
  global_secondary_index {
    name            = "email-index"
    hash_key        = "email"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}

# Parsing feedback - original parser JSON + user-corrected JSON, captured after
# the review step for model improvement. Contains resume PII, so records are
# TTL-expired (app sets `ttl`; see feedback_retention_days, default 90 days).
resource "aws_dynamodb_table" "feedback" {
  name         = "resume-parser-feedback"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "feedback_id"

  attribute {
    name = "feedback_id"
    type = "S"
  }

  attribute {
    name = "company_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # Batch-export a company's corrections over a time range for model improvement.
  global_secondary_index {
    name            = "company-created-index"
    hash_key        = "company_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}
