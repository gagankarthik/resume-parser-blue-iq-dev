# ── DynamoDB Tables ───────────────────────────────────────────────────────────
# All tables use PAY_PER_REQUEST (on-demand) — no capacity planning needed.
# TTL is enabled where applicable for automatic cleanup.

resource "aws_dynamodb_table" "api_keys" {
  name         = "resume-parser-api-keys"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key_hash"

  attribute {
    name = "key_hash"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}

resource "aws_dynamodb_table" "rate_limits" {
  name         = "resume-parser-rate-limits"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "window_key"

  attribute {
    name = "window_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

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

  # Audit logs retained 90 days
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }
  tags = local.common_tags
}
