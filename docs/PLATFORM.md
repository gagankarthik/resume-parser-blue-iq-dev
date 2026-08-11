# Blue-IQ Resume Parser - Platform Handbook

**The single source of truth for all three surfaces of the platform.** Setup,
environment, deployment, auth, design and analytics live here, once. Each repo's
own `README.md` is a short product page that links back to this document rather
than repeating it - if you are about to document something in a repo README, it
almost certainly belongs here instead.

| Document | What it covers |
|---|---|
| **This file** | Platform overview, all three surfaces, environment, deploy, operations |
| [`DESIGN.md`](./DESIGN.md) | The visual language both front ends share |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md), [`DEPLOYMENT.md`](./DEPLOYMENT.md), [`CLIENT_INTEGRATION_GUIDE.md`](./CLIENT_INTEGRATION_GUIDE.md) | Deep backend references |

---

## 1. What the platform is

Blue-IQ Resume Parser turns a healthcare resume (PDF, DOCX, RTF, or a scanned
image) into structured, catalog-mapped JSON: contact details, every work-history
role with its facility and clinical specialties, education, licences and
certifications - each with a confidence score.

It is built for healthcare staffing. That shows up in the details that generic
parsers get wrong: a travel assignment is split into one entry per facility
rather than collapsed under the agency; specialties resolve to the client
platform's **exact** profession-scoped IDs (ICU is `56` for an RN and `757` for a
CNA); and anything the parser cannot confidently map is surfaced for review
rather than guessed.

### The three surfaces

| Surface | Repo | What it is |
|---|---|---|
| **Parser API** | `resume-parser-blue-iq-dev` | FastAPI on AWS Lambda. The engine and the public API. |
| **Product platform** | `resume-parser-ui-blue-iq-dev` | The customer-facing Next.js app: marketing site, docs, and the dashboard where customers manage API keys, webhooks and usage. |
| **UAT console** | `uat-testing-ui-blue-iq` | The internal Next.js operator console: exercise every endpoint against live infrastructure, inspect parsed output field by field, browse DynamoDB, and read production analytics. |

Both front ends are Next.js (App Router, SSR) deployed on AWS Amplify managed
compute. Neither is a static export - both use server components, cookie
sessions and `app/api/*` routes.

---

## 2. The parse contract

**Every parse is asynchronous.** There is no synchronous mode: submit a file,
receive a `job_id`, poll until the status is terminal. This is uniform for a
50 KB DOCX and a 40-page scanned PDF, so integrators write one code path.

```bash
# 1. Submit
curl -X POST https://api.parsinglab.blue-iq.ai/api/v1/resume/parse \
  -H "X-API-Key: $KEY" -F "file=@resume.pdf"
# -> { "job_id": "01K...", "status": "processing", "poll_url": "/api/v1/resume/job/01K..." }

# 2. Poll
curl https://api.parsinglab.blue-iq.ai/api/v1/resume/job/01K... -H "X-API-Key: $KEY"
# -> { "status": "completed", "data": { ... }, "confidence": { ... }, "partial": false }
```

`status` is one of `processing`, `completed`, `partial` or `failed`. `partial`
means the parse degraded (for example the AI stage timed out and only contact
anchors were recovered) - the payload is still usable, but flag it for review.

Webhooks are the alternative to polling. **Secrets are per registration**, every
active hook receives every event, and a `401` from your endpoint is never
retried - see the backend docs for the full delivery contract.

> **Job results carry a TTL.** The `resume-parser-jobs` table expires rows, so a
> `job_id` is not a permanent handle. Anything you need to keep, store on your
> side. The durable record of *that a parse happened* is the audit-log table,
> which is what the analytics in §6 are built on.

---

## 3. Repository layout

Three independent Git repositories, each self-contained and separately
deployable:

```
Blue-IQ Resume Parser/
├-- resume-parser-blue-iq-dev/     <- FastAPI parser + Terraform + these docs
│   └-- docs/PLATFORM.md           <- you are here
├-- resume-parser-ui-blue-iq-dev/  <- customer product platform (Next.js)
└-- uat-testing-ui-blue-iq/        <- internal UAT console (Next.js)
```

Each front end owns its own `components/` and `app/globals.css` - there is no
shared package and no build-time coupling between the repos. They are written to
*look* identical; see [`DESIGN.md`](./DESIGN.md) for what must be kept in step.

---

## 4. Environment

### Product platform - `resume-parser-ui-blue-iq-dev`

| Var | Purpose |
|---|---|
| `BACKEND_API_URL` | Base URL of the parser backend (production: `https://api.parsinglab.blue-iq.ai`) |
| `ADMIN_API_TOKEN` | Shared secret sent as `X-Admin-Token`; must match the backend's `ADMIN_API_TOKEN` |
| `NEXT_PUBLIC_COGNITO_USER_POOL_ID` / `NEXT_PUBLIC_COGNITO_CLIENT_ID` | Cognito IDs (public; used by the browser SDK and server-side token verification) |
| `NEXT_PUBLIC_API_BASE_URL` | Public API base URL shown in `/docs` samples (optional) |
| `NEXT_PUBLIC_SITE_URL` | **Required in production.** The public origin of this site. Canonical URLs, Open Graph tags, `robots.txt` and `sitemap.xml` are all resolved against it - left unset they fall back to `http://localhost:3000`, which would publish localhost URLs to crawlers. |

### UAT console - `uat-testing-ui-blue-iq`

| Var | Purpose |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | Parser API to test against |
| `RESUME_PARSER_API_KEY` | Sent upstream as `X-API-Key`. Never commit a real key. |
| `NEXT_PUBLIC_COGNITO_USER_POOL_ID` / `NEXT_PUBLIC_COGNITO_CLIENT_ID` | Same Cognito pool as the product platform |
| `ADMIN_EMAILS` | Comma-separated operator emails allowed into `/admin` and `/analytics` |
| `NEXT_PUBLIC_AWS_REGION` / `NEXT_PUBLIC_AWS_ACCESS_KEY_ID` / `NEXT_PUBLIC_AWS_SECRET_ACCESS_KEY` | Used **server-side only** to read DynamoDB and CloudWatch. Use a read-only key. |
| `DYNAMODB_TABLE_*` | Optional table-name overrides (default `resume-parser-*`) |
| `LAMBDA_API_FUNCTION` / `LAMBDA_WORKER_FUNCTION` | Optional Lambda name overrides for the infra panel (defaults `resume-parser-production-api` / `-worker`) |

**Cognito pool requirements** (shared by both sites): email as the sign-in
identifier, a public app client (no secret - the SDK uses SRP), and the
`email` / `name` attributes.

**IAM for the UAT console.** The credentials above need read access to the
DynamoDB tables *and* `cloudwatch:GetMetricStatistics` for the infrastructure
panel. Without the CloudWatch permission the product metrics still render and
the infra panel degrades to an "unavailable" state rather than failing the page.

---

## 5. Local development

```bash
# either front end
cp .env.example .env.local
npm install
npm run dev            # http://localhost:3000

# backend
cd resume-parser-blue-iq-dev
poetry install
make dev
```

Node 20+ is required (Next.js 16); `amplify.yml` pins it via `nvm`.

**Before committing UI changes**, verify the app builds:

```bash
npm run verify        # tsc --noEmit && next build
npm run lint
```

If you changed anything visual, check [`DESIGN.md`](./DESIGN.md) for whether the
other front end needs the same change to stay consistent.

---

## 6. The UAT analytics surface

`/analytics` in the UAT console (operators only) reports **real production
data** - there is no seeded or sample path, so an empty window renders an empty
state rather than invented numbers.

| Panel | Source |
|---|---|
| Parses/day, tokens/day, outcome mix, file types, busiest customers, error codes, slowest parses | `resume-parser-audit-logs` (the durable record of every parse) |
| Latency p50/p95 by day, success rate, OCR rate | Same table, aggregated server-side |
| Invocations, errors, error rate, throttles, duration | CloudWatch `AWS/Lambda` for the API and async-worker functions |

The **jobs** table is deliberately not used as an analytics source: its rows
expire, so it cannot answer questions about last month.

---

## 7. Deploying

Both front ends deploy the same way. **Amplify hosts them on managed compute
(`WEB_COMPUTE`) - never switch either to static export.**

1. **Connect the repo.** Amplify Console -> *New app* -> *Host web app* -> GitHub ->
   pick the repo and branch. Amplify auto-detects Next.js SSR and uses the
   committed `amplify.yml`.
2. **Set environment variables** (App settings -> Environment variables) from §4.
   `NEXT_PUBLIC_*` values are inlined at build time - `amplify.yml` writes them
   into `.env.production`; everything else stays server-side.
3. **Deploy.** Each push to the connected branch triggers a build and deploy.
4. **After the first deploy**, add the Amplify app URL to the Cognito app
   client's allowed callback/sign-out URLs, and allow it as an origin on the
   backend admin API.

The backend deploys separately via Terraform in `infrastructure/` - see
[`DEPLOYMENT.md`](./DEPLOYMENT.md).

---

## 8. Operational notes

- **The async worker is required.** A deploy without the SQS worker queue must
  fail the deploy, not the service - a missing worker previously caused parses to
  block the HTTP response for ~22 seconds.
- **The specialty catalog is a bundled snapshot.** The request path never fetches
  it live. Re-sync with `python -m scripts.refresh_specialty_catalog` and commit
  the regenerated JSON when the client updates their catalog.
- **Learned refinement rules are optional.** The parser will append approved
  agent rules from the `resume-parser-agent-instructions` table to its prompts
  when that table exists; when it does not, the loader logs and treats it as "no
  rules". Rules only ever go live through a deliberate admin approval - there is
  no auto-approve path.
