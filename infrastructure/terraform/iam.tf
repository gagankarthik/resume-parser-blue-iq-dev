# -- Lambda execution role -----------------------------------------------------

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${local.name_prefix}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

# CloudWatch Logs
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Application permissions - least-privilege
data "aws_iam_policy_document" "lambda_app" {
  # DynamoDB - all application tables (and their GSIs)
  statement {
    sid    = "DynamoDB"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
      "dynamodb:BatchWriteItem", "dynamodb:DescribeTable",
    ]
    resources = [
      aws_dynamodb_table.api_keys.arn,
      "${aws_dynamodb_table.api_keys.arn}/index/*",
      aws_dynamodb_table.jobs.arn,
      aws_dynamodb_table.batches.arn,
      aws_dynamodb_table.webhooks.arn,
      "${aws_dynamodb_table.webhooks.arn}/index/*",
      aws_dynamodb_table.audit_logs.arn,
      "${aws_dynamodb_table.audit_logs.arn}/index/*",
      aws_dynamodb_table.companies.arn,
      "${aws_dynamodb_table.companies.arn}/index/*",
      aws_dynamodb_table.feedback.arn,
      "${aws_dynamodb_table.feedback.arn}/index/*",
    ]
  }

  # S3 - temp bucket only.
  # Note: the HeadBucket API call (used by /health) is authorized by
  # s3:ListBucket - there is no separate s3:HeadBucket IAM action.
  statement {
    sid    = "S3Temp"
    effect = "Allow"
    actions = [
      "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.temp.arn,
      "${aws_s3_bucket.temp.arn}/*",
    ]
  }

  # Textract - real AWS only, no LocalStack
  statement {
    sid       = "Textract"
    effect    = "Allow"
    actions   = ["textract:DetectDocumentText", "textract:AnalyzeDocument"]
    resources = ["*"]
  }

  # Lambda - the function invokes ITSELF for async OCR work.
  # ARN is constructed (not a resource reference) to avoid a dependency cycle:
  # function -> exec-role policy -> function.
  statement {
    sid     = "InvokeSelf"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-api",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_app" {
  name   = "${local.name_prefix}-app-policy"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_app.json
}

# -- GitHub Actions deployment role --------------------------------------------

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"]
    }
    # Restrict to main branch pushes and the production environment only.
    # Using StringEquals (not StringLike) with explicit refs prevents fork PRs
    # and arbitrary branches from assuming this role.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/main",
        "repo:${var.github_owner}/${var.github_repo}:environment:production",
      ]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${local.name_prefix}-github-actions-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid    = "ECRAuth"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ECRPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
      "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid    = "LambdaDeploy"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:GetFunction",
      "lambda:PublishVersion",
    ]
    resources = [
      aws_lambda_function.api.arn,
    ]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "${local.name_prefix}-github-actions-deploy"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}
