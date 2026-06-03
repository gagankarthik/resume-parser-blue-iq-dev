from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # App
    app_name: str = "Resume Parser API"
    app_version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False

    # AWS
    aws_region: str = "us-east-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    dynamodb_endpoint_url: str = ""  # empty = real AWS
    s3_endpoint_url: str = ""        # empty = real AWS
    textract_endpoint_url: str = ""  # always real AWS Textract

    # DynamoDB tables
    dynamodb_table_api_keys: str = "resume-parser-api-keys"
    dynamodb_table_jobs: str = "resume-parser-jobs"
    dynamodb_table_batches: str = "resume-parser-batches"
    dynamodb_table_webhooks: str = "resume-parser-webhooks"
    dynamodb_table_audit_logs: str = "resume-parser-audit-logs"
    dynamodb_table_companies: str = "resume-parser-companies"

    # GSI names (used by usage/stats and onboarding queries)
    audit_logs_company_index: str = "company-timestamp-index"
    companies_email_index: str = "email-index"
    api_keys_company_index: str = "company-index"

    # Admin API — token that gates the /api/v1/admin/* endpoints used by the
    # product platform (company onboarding, key management, usage stats).
    # Set a strong value in production; empty disables the admin endpoints.
    admin_api_token: str = ""

    # Secret for signing self-serve account session tokens (/api/v1/auth).
    # MUST be set to a strong random value in production.
    auth_secret: str = "dev-insecure-auth-secret-change-me"

    # S3
    s3_bucket_name: str = "resume-parser-temp"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_max_tokens: int = 4096

    # Processing limits
    # NOTE: this is the application-level cap. The Lambda Function URL fronting the
    # API still hard-caps the request payload at ~6 MB (the synchronous invoke
    # limit), so uploads between ~6 MB and this value will be rejected at the AWS
    # edge before reaching the app. To genuinely support 10 MB, switch large
    # uploads to a presigned-S3 flow (client → S3, then call the API with the key).
    max_file_size_mb: int = 10
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
