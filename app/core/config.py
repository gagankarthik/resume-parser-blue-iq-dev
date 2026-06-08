from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # App
    app_name: str = "Resume Parser API"
    app_version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False

    # AWS
    # On Lambda, AWS_REGION is a reserved runtime variable we cannot override, so
    # Terraform passes the region as AWS_REGION_NAME. Accept either — AWS_REGION
    # (auto-set by Lambda / used in dev) or AWS_REGION_NAME (Terraform) — so the
    # client region is always explicit instead of relying on an implicit default.
    aws_region: str = Field(
        default="us-east-2",
        validation_alias=AliasChoices("aws_region", "aws_region_name"),
    )
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    dynamodb_endpoint_url: str = ""  # empty = real AWS
    s3_endpoint_url: str = ""        # empty = real AWS
    textract_endpoint_url: str = ""  # always real AWS Textract

    # OCR
    # When True, the OCR path skips Tesseract entirely and goes straight to AWS
    # Textract for every scanned PDF/image. Leave False to keep the cost-saving
    # tiered behaviour (Tesseract first, Textract only when confidence is low).
    # Callers can also force Textract per-request via the `force_textract` flag on
    # the parse endpoints, which OR's with this global default.
    force_textract: bool = False

    # DynamoDB tables
    dynamodb_table_api_keys: str = "resume-parser-api-keys"
    dynamodb_table_jobs: str = "resume-parser-jobs"
    dynamodb_table_batches: str = "resume-parser-batches"
    dynamodb_table_webhooks: str = "resume-parser-webhooks"
    dynamodb_table_audit_logs: str = "resume-parser-audit-logs"
    dynamodb_table_companies: str = "resume-parser-companies"
    dynamodb_table_feedback: str = "resume-parser-feedback"

    # GSI names (used by usage/stats and onboarding queries)
    audit_logs_company_index: str = "company-timestamp-index"
    companies_email_index: str = "email-index"
    api_keys_company_index: str = "company-index"
    feedback_company_index: str = "company-created-index"

    # How long parsing feedback (original + corrected JSON) is retained before
    # the DynamoDB TTL expires it. Feedback carries resume PII, so it is not kept
    # indefinitely; 90 days is enough to batch-export for model improvement.
    feedback_retention_days: int = 90

    # Admin API — token that gates the /api/v1/admin/* endpoints used by the
    # product platform (company onboarding, key management, usage stats).
    # Set a strong value in production; empty disables the admin endpoints.
    admin_api_token: str = ""

    # Secret for signing self-serve account session tokens (/api/v1/auth).
    # MUST be set to a strong random value in production.
    auth_secret: str = "dev-insecure-auth-secret-change-me"

    # S3
    # Must match the bucket Terraform provisions (infrastructure/terraform/s3.tf).
    # Kept in sync with .env.example; overridden in production via S3_BUCKET_NAME.
    s3_bucket_name: str = "resume-parser-blue-iq-temp"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_max_tokens: int = 4096

    # Multi-agent parser
    # When True, resumes are parsed by the multi-agent orchestrator (structure →
    # per-role work extraction → parallel section agents → bullet-count validation)
    # instead of one single-shot LLM call. Higher accuracy on long / travel-nurse
    # résumés at the cost of more LLM calls; the single-shot parser remains the
    # automatic fallback if the orchestrator fails.
    use_multi_agent: bool = True
    # Cap on concurrent in-flight LLM calls across the whole pipeline (Stage-2
    # agents + per-role WorkAgent calls) so long résumés don't trip the TPM ceiling.
    multi_agent_max_concurrency: int = 4
    # Complexity gate: résumés with at least this many cleaned characters use the
    # multi-agent orchestrator; shorter/simpler ones use the fast single-shot
    # parser. This keeps the synchronous path snappy for one-page résumés while
    # reserving the costlier multi-agent accuracy for long / travel-nurse CVs that
    # actually benefit. Set to 0 to always use multi-agent (when enabled).
    multi_agent_min_chars: int = 3500

    # Processing limits
    # The direct multipart endpoint (POST /resume/parse) is still bounded by the
    # Lambda Function URL's ~6 MB request cap, so files larger than that must use
    # the presigned-upload flow (POST /resume/upload-url → client uploads straight
    # to S3 → POST /resume/parse-uploaded), which supports the full size below.
    max_file_size_mb: int = 10
    # How long a presigned upload URL stays valid (seconds).
    presigned_upload_expiry_seconds: int = 900  # 15 minutes
    job_result_ttl_seconds: int = 3600  # 1 hour

    # CORS — comma-separated allowed origins. Empty by default so production
    # denies cross-origin browser access unless an operator opts in via
    # CORS_ALLOWED_ORIGINS. Development falls back to "*" for convenience
    # (see app/main.py). Note: the test UI talks to the API through its own
    # server-side proxy, so it does not rely on CORS at all.
    cors_allowed_origins: str = ""

    # Webhooks
    webhook_timeout_seconds: int = 10
    webhook_max_retries: int = 3

    # Batch processing
    max_batch_size: int = 200           # maximum files per batch request
    max_concurrent_jobs: int = 5        # concurrent pipeline runs (local dev semaphore)
    max_retry_count: int = 3            # maximum retry attempts per job
                                        # on Lambda, control via reserved concurrency

    # Lambda (async worker)
    # Set to the Lambda function name in production; leave empty to use BackgroundTasks in dev
    worker_lambda_function_name: str = ""

    @property
    def use_lambda_worker(self) -> bool:
        """True when running in Lambda and a worker function is configured."""
        return bool(self.worker_lambda_function_name)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
