# Documentation

Technical and operational documentation for the **Resume Parser API**. The product overview lives
in the [root `README.md`](../README.md); everything engineering-facing lives here.

## Index

| Document | Audience | What's inside |
| --- | --- | --- |
| [`PROJECT.md`](./PROJECT.md) | Engineers | Mission, the non-negotiable invariants, the parse ladder, and the rules for changing the system without degrading it. **Start here to work on the code.** |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Engineers / reviewers | Design principles, compute model, processing pipeline, data layer, security, scalability. |
| [`DEPLOYMENT.md`](./DEPLOYMENT.md) | Ops / infra | CI/CD pipeline, AWS services, OpenAI configuration, the async-worker (SQS) provisioning reality, and the deploy/rollback runbook. |
| [`CLIENT_INTEGRATION_GUIDE.md`](./CLIENT_INTEGRATION_GUIDE.md) | API consumers | Auth, sync vs. async parsing, polling, webhooks, rate limits, Python/Node examples, checklist. |
| [`custom-api-domain.md`](./custom-api-domain.md) | Ops / infra | The `api.parsinglab.blue-iq.ai` custom domain: CloudFront + ACM setup, runbook, troubleshooting. |
| [`ocean-blue-integration-flow.md`](./ocean-blue-integration-flow.md) | Integration partners | End-to-end frontend → backend → API flow (swimlane diagram + Mermaid source). |
| [`CLEANUP_PLAN.md`](./CLEANUP_PLAN.md) | Engineers | Tracked technical debt and the Terraform-adoption plan. |

There is also a client-facing **Knowledge Book** (`Blue-IQ_Resume_Parser_Knowledge_Book.docx`) in
this folder — a narrative walkthrough of the product for non-engineering stakeholders.

## Quick links by task

- **Call the API** → [`CLIENT_INTEGRATION_GUIDE.md`](./CLIENT_INTEGRATION_GUIDE.md)
- **Understand the design** → [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- **Deploy or operate it** → [`DEPLOYMENT.md`](./DEPLOYMENT.md)
- **Change the code safely** → [`PROJECT.md`](./PROJECT.md)
- **Set up the custom domain** → [`custom-api-domain.md`](./custom-api-domain.md)

## Source data

The healthcare taxonomy used for skills validation and specialty grouping was hand-derived from
GigHealth's professions/specialties reference and is baked into
`app/services/normalization/healthcare_taxonomy.py` and `taxonomy_data.py`. The platform catalogs
(specialty, facility, geography) are refreshed from the GigHealth Partner API into `app/data/`
snapshots via `python -m scripts.refresh_*_catalog`.
