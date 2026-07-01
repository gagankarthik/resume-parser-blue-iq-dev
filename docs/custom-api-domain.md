# Custom API Domain — `api.parsinglab.blue-iq.ai`

How the Resume Parser API is exposed on a branded HTTPS domain, why we moved off
the auto-generated AWS endpoints, and how to set it up or operate it.

- **Public API base URL:** `https://api.parsinglab.blue-iq.ai`
- **AWS account:** `417915984158` · **Backend region:** `us-east-2`
- **Fronted by:** CloudFront `E3GTNV4GH38H1K` (`dvmt67rge7ny6.cloudfront.net`)
- **TLS cert:** ACM `…/e153db11-28bd-412e-8248-83beff2feb54` (us-east-1)
- **DNS:** GoDaddy (zone `blue-iq.ai`, nameservers `ns2x.domaincontrol.com`)

---

## 1. Background — what we migrated from

The Lambda `resume-parser-production-api` was reachable two ways, neither of them
a clean branded endpoint:

| Endpoint | What it was | Problem |
|---|---|---|
| `https://dqzxww…lambda-url.us-east-2.on.aws` | Lambda **Function URL** | Works, but an opaque, unbrandable AWS hostname. Can't attach a custom domain directly. |
| `https://vg5to7qyn0.execute-api.us-east-2.amazonaws.com/default/resume-parser-production-api` | Auto-created **HTTP API Gateway** trigger | **Broken** — single route `ANY /resume-parser-production-api`, so every real path (`/api/v1/...`) returned **404**. |

The apps pointed at the raw Function URL. The API Gateway was a leftover from a
console "Add trigger" click and never routed correctly.

**Goal:** one branded endpoint — `api.parsinglab.blue-iq.ai` — mirroring the UI at
`www.parsinglab.blue-iq.ai`, with valid TLS, and remove the dead API Gateway.

### Why CloudFront (not API Gateway)

A Lambda **Function URL cannot take a custom domain on its own**. The two ways to
put a domain in front of it are:

1. **CloudFront + ACM cert** (chosen) — keep the existing Function URL as the
   origin, add edge TLS + the hostname. No app changes, no request/response
   reshaping, cheapest path.
2. **Migrate to API Gateway custom domain** — would mean re-platforming the
   trigger, re-testing routing/limits, and paying per-request API Gateway costs.

We chose CloudFront: it's additive, leaves the app and its `X-API-Key` auth
untouched, and the Function URL keeps working as a fallback/origin.

---

## 2. How it works

```
Client (browser / server / curl)
        │  HTTPS  https://api.parsinglab.blue-iq.ai/api/v1/resume/parse
        ▼
GoDaddy DNS  ──  CNAME api.parsinglab → dvmt67rge7ny6.cloudfront.net
        ▼
CloudFront  E3GTNV4GH38H1K
   • TLS terminated with ACM cert (*api.parsinglab.blue-iq.ai*, us-east-1)
   • Cache policy: CachingDisabled       (never cache API responses)
   • Origin req policy: AllViewerExceptHostHeader
        │  HTTPS (origin-only)  forwards path, query, body,
        │  X-API-Key, Authorization — everything except Host
        ▼
Lambda Function URL  dqzxww….lambda-url.us-east-2.on.aws  (auth type NONE)
        ▼
FastAPI app (Mangum)  →  owns X-API-Key auth + CORS  →  DynamoDB / S3 / OpenAI
```

Key point: **CloudFront is a transparent pass-through.** It adds a hostname and
edge TLS; the FastAPI application still owns authentication (`X-API-Key`) and CORS.

### Why these two CloudFront policies

- **CachingDisabled** — this is an API, not static content. Every request must hit
  the origin; nothing may be cached or coalesced.
- **AllViewerExceptHostHeader** — forwards *all* viewer headers, query string, and
  body to the origin **except `Host`**. This is essential: a Lambda Function URL
  rejects requests whose `Host` header doesn't match its own hostname. Stripping
  `Host` lets CloudFront send the origin's expected host while still passing
  `X-API-Key`, `Authorization`, cookies, etc.

---

## 3. The AWS resources

| Resource | ID / value | Region | Notes |
|---|---|---|---|
| ACM certificate | `…/e153db11-28bd-412e-8248-83beff2feb54` | **us-east-1** | DNS-validated. CloudFront certs must be in us-east-1. |
| CloudFront distribution | `E3GTNV4GH38H1K` → `dvmt67rge7ny6.cloudfront.net` | global | Alias `api.parsinglab.blue-iq.ai`, PriceClass_100. |
| Origin | `dqzxww…lambda-url.us-east-2.on.aws` | us-east-2 | The existing Lambda Function URL. |
| GoDaddy CNAME (validation) | `_…​.api.parsinglab` → `_….acm-validations.aws` | — | Added once; proves domain ownership to ACM. |
| GoDaddy CNAME (traffic) | `api.parsinglab` → `dvmt67rge7ny6.cloudfront.net` | — | Routes the domain to CloudFront. |

---

## 4. Setup from scratch (runbook)

DNS lives at **GoDaddy**, so cert validation and the final record are added by
hand there. Certs and CloudFront are created in AWS.

### 4.1 Request the ACM certificate (us-east-1)

```bash
aws acm request-certificate \
  --region us-east-1 \
  --domain-name api.parsinglab.blue-iq.ai \
  --validation-method DNS \
  --query CertificateArn --output text

# Get the DNS validation record to add at GoDaddy:
aws acm describe-certificate --region us-east-1 --certificate-arn <ARN> \
  --query "Certificate.DomainValidationOptions[0].ResourceRecord"
```

In **GoDaddy** (zone `blue-iq.ai`) add a **CNAME**:

- **Host:** the record `Name` minus `.blue-iq.ai` (e.g. `_abc123.api.parsinglab`)
- **Value:** the record `Value` (e.g. `_xyz.jkddzz.acm-validations.aws`), no trailing dot

Wait until the cert is `ISSUED` (a few minutes):

```bash
aws acm describe-certificate --region us-east-1 --certificate-arn <ARN> \
  --query "Certificate.Status" --output text
```

> **Wildcard note:** the existing `*.blue-iq.ai` cert does **not** cover
> `api.parsinglab.blue-iq.ai` — a wildcard matches only one label, and this name is
> two labels under `blue-iq.ai`. Hence a dedicated cert.

### 4.2 Create the CloudFront distribution

Origin = the Function URL host (no scheme, no trailing slash). Use the managed
policies **CachingDisabled** (`4135ea2d-6df8-44a3-9df3-4b5a84be39ad`) and
**AllViewerExceptHostHeader** (`b689b0a8-53d0-40ab-baf2-68738e2966ac`), alias =
the domain, and the ACM cert from 4.1. (See the Terraform in §6 for the exact
shape; it was created imperatively here via `aws cloudfront create-distribution`.)

### 4.3 Point the domain at CloudFront

In **GoDaddy**, add the traffic **CNAME**:

- **Host:** `api.parsinglab`
- **Value:** `dvmt67rge7ny6.cloudfront.net`

CloudFront takes ~5–15 min to deploy. Then verify:

```bash
curl https://api.parsinglab.blue-iq.ai/api/v1/health
# → {"status":"ok","version":"1.0.0","environment":"production",...}
```

### 4.4 Wire the apps to the domain

| App | Where | Variable → value |
|---|---|---|
| Product UI (`resume-parser-ui-blue-iq-dev`, Amplify `dzn5afwe6s1rs`) | Amplify → Environment variables | `BACKEND_API_URL` = `https://api.parsinglab.blue-iq.ai`  ·  `NEXT_PUBLIC_API_BASE_URL` = `https://api.parsinglab.blue-iq.ai` |
| Product UI code default | `lib/config.ts` | `API_BASE` falls back to `https://api.parsinglab.blue-iq.ai` |
| UAT console (`uat-testing-ui-blue-iq`) | its `.env` / host | `NEXT_PUBLIC_API_BASE_URL` = `https://api.parsinglab.blue-iq.ai` |

Amplify inlines `NEXT_PUBLIC_*` at **build time**, so after changing env vars you
must trigger a rebuild:

```bash
aws amplify start-job --region us-east-2 --app-id dzn5afwe6s1rs \
  --branch-name main --job-type RELEASE
```

### 4.5 Remove the dead API Gateway

```bash
# The stray HTTP API that 404'd:
aws apigatewayv2 delete-api --region us-east-2 --api-id vg5to7qyn0

# Remove the now-orphaned invoke permission it left on the Lambda
# (keep FunctionURLAllowPublicAccess):
aws lambda get-policy --region us-east-2 --function-name resume-parser-production-api
aws lambda remove-permission --region us-east-2 \
  --function-name resume-parser-production-api --statement-id <the-apigw-statement-id>
```

---

## 5. Verification / smoke tests

```bash
# Health via the domain (expect HTTP 200 + JSON)
curl -s -o /dev/null -w "%{http_code}\n" https://api.parsinglab.blue-iq.ai/api/v1/health

# TLS is valid (ssl_verify=0 means OK)
curl -s -o /dev/null -w "ssl=%{ssl_verify_result}\n" https://api.parsinglab.blue-iq.ai/api/v1/health

# DNS resolves to CloudFront
nslookup api.parsinglab.blue-iq.ai   # → CNAME dvmt67rge7ny6.cloudfront.net

# The old API Gateway is gone (expect it not to resolve / connect)
curl -s -o /dev/null -w "%{http_code}\n" \
  https://vg5to7qyn0.execute-api.us-east-2.amazonaws.com/default/resume-parser-production-api
```

---

## 6. Infrastructure as Code

The equivalent Terraform lives in
[`infrastructure/terraform/cloudfront_api.tf`](../infrastructure/terraform/cloudfront_api.tf).
It is gated on the `api_custom_domain` variable (empty = disabled) and mirrors the
resources above: us-east-1 provider alias, DNS-validated ACM cert, and the
CloudFront distribution.

**Two-phase apply** (because DNS is external at GoDaddy):

```bash
cd infrastructure/terraform
# 1) create the cert, then add its validation CNAME at GoDaddy
terraform apply -target=aws_acm_certificate.api
terraform output api_cert_validation_records

# 2) once the cert is ISSUED, create the distribution
terraform apply
terraform output cloudfront_domain_name   # → CNAME api.parsinglab → this
```

> **State caveat:** the live resources documented here were created imperatively
> via the AWS CLI, and this Terraform stack's S3 backend
> (`resume-parser-tfstate`) does not yet exist — so nothing is currently
> Terraform-managed. To bring it under IaC: create the state bucket + lock table,
> `terraform init`, then `terraform import` the existing cert and distribution
> before the next apply. Until then, treat `cloudfront_api.tf` as the reference
> definition, not the source of truth.

---

## 7. Operations & troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `403` with a Host/SignatureDoesNotMatch style error | `Host` header reaching the Function URL | Ensure the origin request policy is **AllViewerExceptHostHeader**, not one that forwards `Host`. |
| Cert stuck `PENDING_VALIDATION` | Validation CNAME missing/typo'd at GoDaddy, or trailing dot included | Re-check the record `Name`/`Value`; GoDaddy host must omit `.blue-iq.ai` and the value must have no trailing dot. |
| Domain returns `000` / won't resolve right after setup | DNS/CloudFront still propagating | Wait 5–15 min; CloudFront status must be `Deployed` and the CNAME propagated. |
| Docs page still shows the old URL | Amplify build cached old `NEXT_PUBLIC_*` | Update the env var **and** trigger a new `RELEASE` build (§4.4). |
| Responses look cached/stale | A caching policy crept in | Confirm the default behavior uses **CachingDisabled**. |

### Security notes

- The Lambda **Function URL is still publicly reachable** (auth type `NONE`); the
  app's `X-API-Key` middleware is the real gate, so this is not an exposure — it
  just means the raw URL works alongside the domain.
- To force **all** traffic through the domain/CDN, lock the Function URL to
  CloudFront only: set its auth type to `AWS_IAM` and attach a CloudFront
  **Origin Access Control (OAC)** that SigV4-signs origin requests. Not enabled
  today; it's the recommended hardening follow-up.

### Teardown

```bash
aws cloudfront get-distribution-config --id E3GTNV4GH38H1K   # disable, then delete
aws acm delete-certificate --region us-east-1 --certificate-arn <ARN>
# remove both GoDaddy CNAMEs; revert app env vars to the Function URL
```
