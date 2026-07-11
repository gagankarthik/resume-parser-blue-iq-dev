from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known insecure default for the account-token signing secret. Safe for dev,
# but a production deploy that leaves it in place lets anyone forge account tokens,
# so `assert_production_ready()` refuses to boot on it.
INSECURE_AUTH_SECRET_DEFAULT = "dev-insecure-auth-secret-change-me"


class Settings(BaseSettings):
    # extra="ignore": an unrecognized env var must never crash the whole service
    # on startup (pydantic-settings otherwise raises extra_forbidden, which 500s
    # every request). Unknown vars are ignored; known ones still validate.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

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

    # Hard cap on how many pages of a scanned PDF/multi-page image are rasterized
    # and OCR'd. At 300 DPI each page is several MB of PIL image held in memory, so
    # an unbounded 100-page fax would OOM/timeout the Lambda. Résumés are a few
    # pages; the tail past this is dropped (logged).
    ocr_max_pages: int = 15

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
    # MUST be set to a strong random value in production — enforced at startup by
    # `assert_production_ready()`, which refuses to boot on the dev default.
    auth_secret: str = INSECURE_AUTH_SECRET_DEFAULT

    # S3
    # Must match the bucket Terraform provisions (infrastructure/terraform/s3.tf).
    # Kept in sync with .env.example; overridden in production via S3_BUCKET_NAME.
    s3_bucket_name: str = "resume-parser-blue-iq-temp"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    # Fixed decoding seed. temperature=0 alone is NOT deterministic on gpt-4.1-mini,
    # so a seed (with a pinned model snapshot) makes identical résumés extract
    # consistently run-to-run and keeps regressions reproducible. Pin openai_model
    # to a dated snapshot in production to hold the system_fingerprint stable.
    openai_seed: int = 7
    # Output-token ceiling. A dense résumé (many roles, each with bullets +
    # facility fields) needs a large JSON; a small ceiling truncates the
    # structured output mid-generation on those, dropping the last roles.
    # gpt-4.1-mini allows up to 32768 output tokens — use generous headroom so
    # completeness is never capped. This is a CEILING, not a target: short
    # résumés still finish in ~2k tokens.
    openai_max_tokens: int = 16384

    # Multi-agent parser
    # When True, resumes are parsed by the multi-agent orchestrator (structure →
    # per-role work extraction → parallel section agents → bullet-count validation)
    # instead of one single-shot LLM call. Higher accuracy on long / travel-nurse
    # résumés at the cost of more LLM calls; the single-shot parser remains the
    # automatic fallback if the orchestrator fails.
    use_multi_agent: bool = True
    # Cap on concurrent in-flight LLM calls across the whole pipeline (Stage-2
    # agents + per-role WorkAgent calls) so long résumés don't trip the TPM ceiling.
    # Sized so a long travel-nurse CV (structure + ~12 per-role calls + sections)
    # drains in 2–3 concurrency rounds instead of serialising into a timeout.
    multi_agent_max_concurrency: int = 8
    # Complexity gate: résumés with at least this many cleaned characters use the
    # multi-agent orchestrator; shorter/simpler ones use the fast single-shot
    # parser. Kept LOW because résumé density (roles × bullets) — not input length —
    # drives the single-shot's latency: compact multi-role CVs (e.g. 12 roles in
    # ~2.9K chars, or a dense skills-heavy RN in ~1.3K chars) blow the sync single-
    # shot budget and degrade to the rule floor. Routing them to the orchestrator,
    # which parallelises and returns graceful partials, fixes that; only genuinely
    # sparse one-role résumés (< ~1K chars) stay on the fast single-shot path.
    # Set to 0 to always use multi-agent (when enabled).
    multi_agent_min_chars: int = 1000

    # Specialty → ID matching
    # Path to the specialty reference catalog (JSON list, or CSV) of
    # {id, specialty, full_name, keywords[], group?, profession?} used to map each
    # per-role specialty to a platform specialty id. Defaults to the snapshot
    # bundled at app/data/specialty_catalog.json (generated from the Gig API by
    # scripts/refresh_specialty_catalog.py). When unset/missing, the matcher still
    # resolves canonical specialty NAMES + confidence from the built-in taxonomy
    # but leaves specialty_id null (matched=False) for admin review.
    specialty_catalog_path: str | None = str(
        Path(__file__).resolve().parents[1] / "data" / "specialty_catalog.json"
    )
    # GigHealth specialties API — source of truth for the catalog snapshot. Only
    # the refresh script reads these (never the request hot path). The key comes
    # from GIG_SPECIAILITIES_API_KEY (matching the platform's spelling); the URL
    # has a sensible default and rarely needs overriding.
    gig_specialties_api_url: str = "https://api.gighealth.com/api/v1/external/specialities"
    gig_specialties_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "gig_specialties_api_key", "gig_speciailities_api_key"
        ),
    )
    # Facility → ID matching
    # Path to the facility reference catalog (JSON list, or CSV) of
    # {id, name, health_system?, health_system_id?} used to map each role's
    # employer/facility name to a platform facility id + confidence. Defaults to the
    # snapshot bundled at app/data/facility_catalog.json (generated from the Gig
    # facilities API by scripts/refresh_facility_catalog.py). When unset/missing, the
    # matcher leaves facility_id null (matched=False) for admin review — parsing is
    # never broken by an absent catalog.
    facility_catalog_path: str | None = str(
        Path(__file__).resolve().parents[1] / "data" / "facility_catalog.json"
    )
    # GigHealth facilities API — source of truth for the facility snapshot. Only the
    # refresh script reads this (never the request hot path); it authenticates with
    # the same platform key as the specialties API (gig_specialties_api_key).
    gig_facilities_api_url: str = "https://api.gighealth.com/api/v1/external/facilities"

    # Geography (country/state) → ID matching
    # Path to the geographies reference snapshot (countries + their states, with
    # platform ids) used to resolve each role's country/state to a platform
    # country_id/state_id offline. Defaults to the snapshot bundled at
    # app/data/geography_catalog.json (generated from the Gig geographies API by
    # scripts/refresh_geography_catalog.py). When unset/missing the ids stay null.
    geography_catalog_path: str | None = str(
        Path(__file__).resolve().parents[1] / "data" / "geography_catalog.json"
    )
    # GigHealth geographies API — source of truth for the geography snapshot. Only
    # the refresh script reads it (never the request hot path).
    gig_geographies_api_url: str = "https://api.gighealth.com/api/v1/external/geographies"

    # City → ID matching (LIVE, opt-in)
    # The cities endpoint is a per-lookup fuzzy search, not bulk reference data, so
    # it cannot be snapshotted. When enabled, the city_resolver enrichment calls it
    # per role (using the offline-resolved country_id/state_id) to stamp city_id +
    # the API score as confidence. ON by default so every résumé gets a city_id
    # alongside facility/geography/specialty ids; it degrades to a no-op when no API
    # key is configured (the country/state ids still resolve offline). Lookups are
    # de-duplicated and capped per résumé. Uses the same platform key as the other
    # Gig endpoints.
    gig_cities_api_url: str = "https://api.gighealth.com/api/v1/external/cities"
    enable_city_api_match: bool = True
    # Upper bound on distinct city lookups per résumé (protects latency + quota).
    city_api_max_lookups: int = 25

    # When True, specialties that miss the deterministic tiers (name/full_name/
    # keywords) are resolved by one batched LLM call against a filtered shortlist
    # from the catalog. No-op when the catalog is empty or nothing is unmatched.
    enable_ai_specialty_match: bool = True
    # Upper bound on how many catalog candidates are offered to the AI shortlist
    # tier in one call (keeps the prompt and token cost bounded).
    specialty_ai_shortlist_max: int = 60

    # Processing limits
    # The direct multipart endpoint (POST /resume/parse) is still bounded by the
    # Lambda Function URL's ~6 MB request cap, so files larger than that must use
    # the presigned-upload flow (POST /resume/upload-url → client uploads straight
    # to S3 → POST /resume/parse-uploaded), which supports the full size below.
    max_file_size_mb: int = 10
    # How long a presigned upload URL stays valid (seconds).
    presigned_upload_expiry_seconds: int = 900  # 15 minutes
    job_result_ttl_seconds: int = 3600  # 1 hour

    # Rate limiting — per-API-key, fixed 60s window, evaluated in the auth
    # dependency so every authenticated request is throttled. BEST-EFFORT per
    # Lambda instance (each warm environment keeps its own counter); front the API
    # with a distributed limiter (API Gateway usage plan / Redis) for a strict
    # global cap.
    # DISABLED by default while the API is under active client testing — flip
    # RATE_LIMIT_ENABLED=true (and tune RATE_LIMIT_PER_MINUTE) to turn it back on.
    rate_limit_enabled: bool = False
    rate_limit_per_minute: int = 600

    # Per-client-IP throttle for the PUBLIC auth routes (/auth/login, /auth/signup).
    # Always enforced — independent of rate_limit_enabled — because brute-force and
    # account-enumeration protection must not be disabled just because per-API-key
    # limiting is off during client testing. Set to 0 to disable.
    auth_rate_limit_per_minute: int = 20

    # Hard upper bound on any accepted request body, checked from Content-Length
    # before the body is read into memory (defense-in-depth above the per-file size
    # check). Sized as the max file size plus multipart/headroom overhead.
    max_request_overhead_bytes: int = 1 * 1024 * 1024

    # A batch request carries MANY files, so the single-file ceiling
    # (max_request_bytes) would wrongly 413 a legitimate multi-file batch. The
    # batch route gets its own, larger body ceiling — still bounded so a huge
    # upload can't exhaust memory. On Lambda the Function URL's ~6 MB cap is the
    # real limit anyway; this matters for non-Lambda (uvicorn/ECS) deploys.
    max_batch_request_bytes: int = 60 * 1024 * 1024

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
    def max_request_bytes(self) -> int:
        """Ceiling for an accepted request body (file size + overhead)."""
        return self.max_file_size_bytes + self.max_request_overhead_bytes

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def assert_production_ready(self) -> None:
        """Refuse to boot a production deployment that is missing critical secrets.

        Called once at app startup (main.lifespan). Today the only fail-closed
        check is the account-token signing secret: an unset/dev-default value in
        production would let anyone forge a session token for any company and mint
        real API keys. (admin_api_token is intentionally optional — empty disables
        the admin endpoints — so it is not enforced here.)
        """
        if not self.is_production:
            return
        if not self.auth_secret or self.auth_secret == INSECURE_AUTH_SECRET_DEFAULT:
            raise RuntimeError(
                "Refusing to start in production: AUTH_SECRET is unset or still the "
                "insecure dev default. Set a strong random value via the environment."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
