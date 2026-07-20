output "api_url" {
  description = "Public API endpoint - share with the client"
  value       = aws_lambda_function_url.api.function_url
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing images"
  value       = aws_ecr_repository.app.repository_url
}

output "api_lambda_arn" {
  description = "Resume-parser API Lambda ARN"
  value       = aws_lambda_function.api.arn
}

output "worker_lambda_arn" {
  description = "Async Worker Lambda ARN"
  value       = aws_lambda_function.worker.arn
}

output "worker_queue_url" {
  description = "Async worker SQS queue URL - the API's WORKER_QUEUE_URL env var points here"
  value       = aws_sqs_queue.worker.url
}

output "worker_dlq_url" {
  description = "Dead-letter queue URL - inspect poison messages that failed past all retries"
  value       = aws_sqs_queue.worker_dlq.url
}

output "github_actions_role_arn" {
  description = "IAM role ARN - paste into GitHub repo secret AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions.arn
}

output "s3_bucket_name" {
  description = "Temp S3 bucket name - paste into .env S3_BUCKET_NAME"
  value       = aws_s3_bucket.temp.bucket
}

# -- Custom API domain (only meaningful when var.api_custom_domain is set) ------

output "api_cert_validation_records" {
  description = "DNS records to add at your DNS provider (GoDaddy) to validate the ACM cert. Add each as a CNAME (name -> value)."
  value = local.api_domain_enabled ? [
    for o in aws_acm_certificate.api[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ] : []
}

output "cloudfront_domain_name" {
  description = "CloudFront hostname - point the custom API domain here (CNAME api.parsinglab.blue-iq.ai -> this value)."
  value       = local.api_domain_enabled ? aws_cloudfront_distribution.api[0].domain_name : null
}

output "api_custom_url" {
  description = "The public API URL on the custom domain (once DNS is in place)."
  value       = local.api_domain_enabled ? "https://${var.api_custom_domain}" : null
}
