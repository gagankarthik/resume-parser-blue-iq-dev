# Secrets stored in SSM Parameter Store — never in Terraform state or env vars
# Lambda reads these at cold start via boto3 SSM.

resource "aws_ssm_parameter" "openai_api_key" {
  name        = "/resume-parser/${var.environment}/openai_api_key"
  description = "OpenAI API key for resume parsing"
  type        = "SecureString"
  value       = var.openai_api_key

  tags = local.common_tags

  lifecycle {
    ignore_changes = [value]  # allow out-of-band rotation without Terraform drift
  }
}
