# Documentation

Documentation for the **Resume Parser API** - an enterprise resume-parsing service that
converts PDF, DOCX, and image resumes into structured JSON, with zero resume-data retention.

The project [`README.md`](../README.md) at the repo root is the primary entry point
(setup, API reference, deployment, schema). The documents below go deeper on specific topics.

## Index

| Document | Audience | What's inside |
| --- | --- | --- |
| [`../README.md`](../README.md) | Everyone | Overview, full API reference, local dev, deployment, output schema, DynamoDB tables. Start here. |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Engineers / reviewers | Enterprise architecture: design principles, compute model, processing pipeline, data layer, security, scalability, CI/CD. |
| [`CLIENT_INTEGRATION_GUIDE.md`](./CLIENT_INTEGRATION_GUIDE.md) | API consumers | Step-by-step integration: auth, sync vs. async parsing, polling, webhooks, rate limits, Python/Node examples, checklist. |
| [`ocean-blue-integration-flow.md`](./ocean-blue-integration-flow.md) | Integration partners | End-to-end frontend -> backend -> API flow (swimlane diagram + editable Mermaid source). |
| [`custom-api-domain.md`](./custom-api-domain.md) | Ops / infra | The `api.parsinglab.blue-iq.ai` custom domain: why we moved off the raw Function URL + API Gateway, how the CloudFront + ACM setup works, setup runbook, IaC, and troubleshooting. |

## Quick links by task

- **I want to call the API** -> [`CLIENT_INTEGRATION_GUIDE.md`](./CLIENT_INTEGRATION_GUIDE.md)
- **I want to run it locally** -> [Local Development](../README.md#local-development) in the root README
- **I want to understand the design** -> [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- **I want the field-by-field output schema** -> [Output Schema](../README.md#output-schema)
- **I want to see the integration flow visually** -> [`ocean-blue-integration-flow.md`](./ocean-blue-integration-flow.md)
- **I want to set up / operate the custom API domain** -> [`custom-api-domain.md`](./custom-api-domain.md)

## Source data

The healthcare taxonomy used for skills validation was hand-derived from
`Professions, Specialities - 2.11.26 updates.xlsx` (repo root) and is baked into
`app/services/normalization/healthcare_taxonomy.py`. The spreadsheet is kept as the
source-of-truth provenance for that data.
