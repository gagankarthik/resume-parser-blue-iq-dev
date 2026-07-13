"""DynamoDB data-access facade.

The table operations now live in per-entity repository modules (api_keys,
companies, jobs, webhooks, audit_logs, feedback, batches). This module re-exports
them so the long-standing ``from app.db import dynamodb as db; db.fn(...)`` call
sites - and `tests/unit/test_dynamo_serialization.py`'s import of the serde
helpers - keep working unchanged.

Tables:
  api_keys      - pk: key_hash
  jobs          - pk: job_id       (async job tracking, TTL 1h)
  batches       - pk: batch_id     (batch tracking, TTL 24h)
  webhooks      - pk: company_id, sk: webhook_id
  audit_logs    - pk: job_id, sk: timestamp
  companies     - pk: company_id  (GSI email-index)
  feedback      - pk: feedback_id (GSI company-created-index, TTL)
"""

from app.db.api_keys import (
    create_api_key,
    get_api_key,
    list_api_keys_for_company,
    revoke_api_key,
)
from app.db.audit_logs import get_audit_logs_for_company, write_audit_log
from app.db.batches import create_batch, get_batch, increment_batch_counter
from app.db.companies import (
    create_company,
    get_company,
    get_company_by_email,
    list_companies,
    update_company,
)
from app.db.feedback import create_feedback, list_feedback_for_company
from app.db.jobs import (
    _dynamo_safe,
    _plain,
    claim_upload_job,
    create_job,
    create_upload_job,
    get_job,
    mark_batch_counted,
    update_job_completed,
    update_job_failed,
    update_job_processing,
)
from app.db.webhooks import (
    create_webhook,
    delete_webhook,
    get_active_webhooks_for_event,
    get_webhook,
    list_webhooks,
)

__all__ = [
    # api_keys
    "get_api_key",
    "create_api_key",
    "revoke_api_key",
    "list_api_keys_for_company",
    # companies
    "create_company",
    "get_company",
    "get_company_by_email",
    "list_companies",
    "update_company",
    # jobs (+ serde helpers)
    "create_job",
    "create_upload_job",
    "claim_upload_job",
    "mark_batch_counted",
    "update_job_processing",
    "update_job_completed",
    "update_job_failed",
    "get_job",
    "_dynamo_safe",
    "_plain",
    # webhooks
    "create_webhook",
    "list_webhooks",
    "get_webhook",
    "delete_webhook",
    "get_active_webhooks_for_event",
    # audit logs
    "write_audit_log",
    "get_audit_logs_for_company",
    # feedback
    "create_feedback",
    "list_feedback_for_company",
    # batches
    "create_batch",
    "get_batch",
    "increment_batch_counter",
]
