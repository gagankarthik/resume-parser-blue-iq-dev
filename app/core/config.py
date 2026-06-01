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
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    dynamodb_endpoint_url: str = ""  # empty = real AWS
    s3_endpoint_url: str = ""        # empty = real AWS
    textract_endpoint_url: str = ""  # always real AWS Textract

    # DynamoDB tables
    dynamodb_table_api_keys: str = "resume-parser-api-keys"
    dynamodb_table_rate_limits: str = "resume-parser-rate-limits"
    dynamodb_table_jobs: str = "resume-parser-jobs"
    dynamodb_table_webhooks: str = "resume-parser-webhooks"
    dynamodb_table_audit_logs: str = "resume-parser-audit-logs"

    # S3
    s3_bucket_name: str = "resume-parser-temp"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_max_tokens: int = 4096

    # Processing limits
    max_file_size_mb: int = 10
    job_result_ttl_seconds: int = 3600  # 1 hour

    # Rate limiting
    default_rate_limit_per_minute: int = 30
    default_rate_limit_per_day: int = 1000

    # Webhooks
    webhook_timeout_seconds: int = 10
    webhook_max_retries: int = 3

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
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
