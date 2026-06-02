# Temp bucket — raw resume files stored only during processing.
# S3 Lifecycle rules delete any objects older than 1 day as a safety net
# (the app deletes files immediately after processing, but this catches leaks).

resource "aws_s3_bucket" "temp" {
  bucket        = "resume-parser-blue-iq-temp"
  force_destroy = false
  tags          = local.common_tags
}

resource "aws_s3_bucket_versioning" "temp" {
  bucket = aws_s3_bucket.temp.id
  versioning_configuration { status = "Disabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "temp" {
  bucket = aws_s3_bucket.temp.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access — resumes must never be publicly accessible
resource "aws_s3_bucket_public_access_block" "temp" {
  bucket                  = aws_s3_bucket.temp.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Safety-net lifecycle: auto-expire any object not deleted by the app
resource "aws_s3_bucket_lifecycle_configuration" "temp" {
  bucket = aws_s3_bucket.temp.id

  rule {
    id     = "expire-temp-files"
    status = "Enabled"

    filter { prefix = "temp/" }

    expiration { days = 1 }
  }
}
