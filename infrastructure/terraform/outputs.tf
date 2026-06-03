output "api_url" {
  description = "Public API endpoint — share with the client"
  value       = aws_lambda_function_url.api.function_url
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing images"
  value       = aws_ecr_repository.app.repository_url
}

output "api_lambda_arn" {
  description = "Resume-parser Lambda ARN"
  value       = aws_lambda_function.api.arn
}

output "github_actions_role_arn" {
  description = "IAM role ARN — paste into GitHub repo secret AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions.arn
}

output "s3_bucket_name" {
  description = "Temp S3 bucket name — paste into .env S3_BUCKET_NAME"
  value       = aws_s3_bucket.temp.bucket
}
