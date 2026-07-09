# Custom API domain: a CloudFront distribution in front of the Lambda Function URL
# so the API is reachable at var.api_custom_domain (e.g. api.parsinglab.blue-iq.ai)
# with a proper TLS cert. The FastAPI app still owns auth (X-API-Key) and CORS —
# CloudFront only adds the hostname + edge TLS and forwards requests through.
#
# Everything here is gated on var.api_custom_domain being set, so the default
# stack (raw Function URL only) is unchanged until you opt in.
#
# DNS is external (GoDaddy), so this is a two-phase apply the first time:
#   1) terraform apply -target=aws_acm_certificate.api
#      terraform output api_cert_validation_records   # add these CNAMEs in GoDaddy
#   2) once the cert validates, terraform apply        # creates the distribution
#      terraform output cloudfront_domain_name         # CNAME api.* → this value

locals {
  api_domain_enabled = var.api_custom_domain != ""
  # Function URL is "https://<host>/"; CloudFront wants just "<host>".
  function_url_host = replace(replace(aws_lambda_function_url.api.function_url, "https://", ""), "/", "")

  # AWS-managed CloudFront policies (stable global IDs).
  cache_policy_caching_disabled     = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  origin_req_all_viewer_except_host = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
}

# ── ACM certificate (us-east-1, DNS-validated) ────────────────────────────────
resource "aws_acm_certificate" "api" {
  count             = local.api_domain_enabled ? 1 : 0
  provider          = aws.us_east_1
  domain_name       = var.api_custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = local.common_tags
}

# Waits until the cert is ISSUED. With external DNS you add the validation CNAME
# (see the api_cert_validation_records output) in GoDaddy; this then completes.
resource "aws_acm_certificate_validation" "api" {
  count                   = local.api_domain_enabled ? 1 : 0
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.api[0].arn
  validation_record_fqdns = [for o in aws_acm_certificate.api[0].domain_validation_options : o.resource_record_name]
}

# ── CloudFront distribution → Lambda Function URL ─────────────────────────────
resource "aws_cloudfront_distribution" "api" {
  count           = local.api_domain_enabled ? 1 : 0
  enabled         = true
  comment         = "${local.name_prefix} API — ${var.api_custom_domain}"
  aliases         = [var.api_custom_domain]
  price_class     = "PriceClass_100" # North America + Europe
  is_ipv6_enabled = true

  origin {
    origin_id   = "lambda-function-url"
    domain_name = local.function_url_host

    custom_origin_config {
      origin_protocol_policy = "https-only"
      http_port              = 80
      https_port             = 443
      origin_ssl_protocols   = ["TLSv1.2"]
      # Dense resumes parse synchronously in 39–55s. The CloudFront default origin
      # response timeout (30s) would sever those before the Lambda responds, so
      # raise it to the max allowed without a quota increase. Requests that need
      # longer should use the async (upload → poll) path.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "lambda-function-url"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # It's an API: never cache, and forward everything except the Host header
    # (Function URL origins reject a mismatched Host). This passes X-API-Key,
    # Authorization, the body, and query string straight through.
    cache_policy_id          = local.cache_policy_caching_disabled
    origin_request_policy_id = local.origin_req_all_viewer_except_host
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.api[0].certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = local.common_tags
}
