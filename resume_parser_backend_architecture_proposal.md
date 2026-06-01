# Resume Parsing Backend Service — Enterprise Architecture Proposal

## Overview

This architecture is designed for a scalable, accurate, and production-grade Resume Parsing Platform.

The system receives resumes from a client frontend, extracts text intelligently from multiple document types, uses AI-based semantic parsing to generate structured JSON, validates and normalizes the data, and returns clean API-ready output that can directly populate frontend forms and databases.

---

# High-Level Architecture

```text
┌────────────────────────────────────────────────────────────┐
│                    CLIENT FRONTEND                         │
│  Upload Resume (PDF / DOCX / Image)                       │
│  View Auto-Filled Candidate Fields                        │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                  API GATEWAY / FASTAPI                     │
│                                                            │
│  Responsibilities:                                         │
│  • Authentication                                           │
│  • Rate Limiting                                            │
│  • File Upload Handling                                     │
│  • Tenant Isolation                                         │
│  • API Versioning                                           │
│  • Webhook Management                                       │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                    FILE STORAGE LAYER                       │
│                                                            │
│  Amazon S3                                                  │
│  • Raw Resume Storage                                       │
│  • Temporary Processing Files                               │
│  • OCR Outputs                                               │
│  • Parsing Logs                                              │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                   JOB QUEUE SYSTEM                          │
│                                                            │
│  Redis + Celery                                             │
│                                                            │
│  Responsibilities:                                          │
│  • Async Resume Processing                                  │
│  • Retry Failed Jobs                                        │
│  • Queue Management                                         │
│  • Parallel Processing                                      │
│  • High Throughput                                          │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                 DOCUMENT CLASSIFICATION                     │
│                                                            │
│  Detect Resume Type:                                       │
│  • PDF                                                      │
│  • DOCX                                                     │
│  • Scanned PDF                                              │
│  • Image Resume                                             │
│                                                            │
│  Decide Parsing Strategy                                    │
└────────────────────────────────────────────────────────────┘
                            │
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼

┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│ PDF TEXT EXTRACTION│ │ DOCX EXTRACTION    │ │ OCR EXTRACTION     │
├────────────────────┤ ├────────────────────┤ ├────────────────────┤
│ PyMuPDF            │ │ python-docx        │ │ Amazon Textract    │
│ pdfplumber         │ │ Apache Tika        │ │ OCR Engine          │
│ Layout Detection   │ │ Mammoth            │ │ Image Preprocessing │
│ Multi-column Parse │ │                    │ │                     │
└────────────────────┘ └────────────────────┘ └────────────────────┘
             │              │              │
             └──────────────┴──────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                   TEXT CLEANING LAYER                       │
│                                                            │
│  Responsibilities:                                         │
│  • Remove OCR Noise                                         │
│  • Normalize Encoding                                       │
│  • Remove Duplicate Text                                    │
│  • Fix Broken Line Structures                               │
│  • Reconstruct Multi-column Layouts                         │
│  • Whitespace Cleanup                                       │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                 SECTION DETECTION ENGINE                    │
│                                                            │
│  Detect Resume Sections:                                   │
│  • Personal Information                                     │
│  • Experience                                                │
│  • Education                                                 │
│  • Skills                                                    │
│  • Projects                                                  │
│  • Certifications                                            │
│  • Achievements                                              │
│                                                            │
│  Techniques:                                                 │
│  • Rule-based Heuristics                                     │
│  • Header Detection                                          │
│  • Lightweight NLP                                           │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│               STRUCTURED AI PARSING LAYER                   │
│                                                            │
│  OpenAI Structured Output Parsing                           │
│                                                            │
│  Responsibilities:                                          │
│  • Semantic Understanding                                   │
│  • Entity Extraction                                        │
│  • Experience Mapping                                       │
│  • Date Association                                         │
│  • Skill Identification                                     │
│  • Job Role Detection                                       │
│  • Education Parsing                                        │
│                                                            │
│  AI Techniques:                                             │
│  • Prompt Engineering                                       │
│  • Function Calling                                         │
│  • JSON Schema Enforcement                                  │
│  • Contextual Parsing                                       │
│  • Chunk-based Processing                                   │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                  PYDANTIC VALIDATION LAYER                  │
│                                                            │
│  Responsibilities:                                          │
│  • JSON Schema Validation                                   │
│  • Data Type Enforcement                                    │
│  • Required Field Validation                                │
│  • Date Validation                                           │
│  • Email Validation                                          │
│  • Phone Validation                                          │
│  • Error Recovery                                            │
│  • Parsing Retry Logic                                       │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                 NORMALIZATION ENGINE                        │
│                                                            │
│  Responsibilities:                                          │
│  • Skill Standardization                                    │
│  • Company Name Cleanup                                     │
│  • Degree Normalization                                     │
│  • Date Formatting                                          │
│  • Country & Location Formatting                            │
│  • Duplicate Removal                                        │
│                                                            │
│  Examples:                                                  │
│  Nodejs → Node.js                                           │
│  MSc → Master of Science                                    │
│  Sr Dev → Senior Developer                                  │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                 CONFIDENCE SCORING ENGINE                   │
│                                                            │
│  Generate Confidence Scores For:                            │
│  • Name Accuracy                                             │
│  • Experience Accuracy                                       │
│  • Skill Detection                                            │
│  • Date Matching                                              │
│                                                            │
│  Purpose:                                                     │
│  • Highlight uncertain fields                                │
│  • Enable human verification                                 │
│  • Improve enterprise trust                                  │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                  STRUCTURED JSON OUTPUT                     │
│                                                            │
│  Output Example:                                            │
│                                                            │
│  {                                                          │
│    "personal_info": {},                                    │
│    "experience": [],                                       │
│    "education": [],                                        │
│    "skills": [],                                           │
│    "projects": [],                                         │
│    "certifications": []                                    │
│  }                                                          │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                  CLIENT APPLICATION                         │
│                                                            │
│  Responsibilities:                                          │
│  • Auto-fill UI Forms                                       │
│  • Allow Manual Corrections                                 │
│  • Save Into Client Database                                │
│  • Candidate Review Workflow                                │
└────────────────────────────────────────────────────────────┘
```

---

# Detailed Technical Components

## 1. API Layer

### Technology
- FastAPI
- JWT Authentication
- OAuth2 Support
- API Key Management

### Features
- REST APIs
- Webhook APIs
- Multi-tenant APIs
- Versioned Endpoints
- Swagger Documentation

---

# 2. File Processing Strategy

| Resume Type | Extraction Method | Reason |
|---|---|---|
| DOCX | python-docx | Native text extraction |
| Digital PDF | PyMuPDF | Better formatting accuracy |
| Scanned PDF | Textract OCR | Image-based extraction |
| Images | OCR Pipeline | Resume image support |

---

# 3. AI Parsing Strategy

## Hybrid AI + Rule-Based Parsing

### Rule-Based Extraction
Used for:
- Emails
- Phone numbers
- LinkedIn URLs
- GitHub URLs

### AI Parsing
Used for:
- Experience extraction
- Education parsing
- Skill mapping
- Job role understanding
- Semantic relationships

---

# 4. Recommended AI Techniques

| Technique | Purpose |
|---|---|
| Structured Outputs | Valid JSON generation |
| Function Calling | Schema enforcement |
| Chunk-based Parsing | Reduce hallucinations |
| Contextual Parsing | Better section understanding |
| Retry Logic | Recover malformed outputs |
| Confidence Scoring | Enterprise reliability |

---

# 5. Security Architecture

```text
┌────────────────────────────────────┐
│           SECURITY LAYER           │
├────────────────────────────────────┤
│ • HTTPS Encryption                 │
│ • JWT Authentication               │
│ • S3 Signed URLs                   │
│ • IAM Role-based Access            │
│ • Encrypted File Storage           │
│ • PII Masking                      │
│ • Audit Logging                    │
│ • Rate Limiting                    │
│ • Tenant Isolation                 │
└────────────────────────────────────┘
```

---

# 6. Scalability Architecture

```text
                    LOAD BALANCER
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   FastAPI Pod 1      FastAPI Pod 2      FastAPI Pod 3
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
                     Redis Queue
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   Worker Node 1      Worker Node 2      Worker Node 3
```

### Scaling Benefits
- Horizontal Scaling
- High Availability
- Fault Tolerance
- Queue-based Reliability
- Parallel Resume Processing

---

# 7. Recommended Database Schema

## Tables

### candidates
- candidate_id
- name
- email
- phone
- location

### experiences
- candidate_id
- company_name
- role
- start_date
- end_date
- description

### education
- candidate_id
- institution
- degree
- graduation_year

### skills
- candidate_id
- skill_name
- confidence_score

### parsing_logs
- job_id
- parsing_status
- error_logs
- processing_time

---

# 8. Suggested API Flow

## Upload Resume

```http
POST /api/v1/resume/upload
```

Response:

```json
{
  "job_id": "abc123",
  "status": "processing"
}
```

---

## Get Parsing Result

```http
GET /api/v1/resume/result/{job_id}
```

Response:

```json
{
  "status": "completed",
  "data": {
    "personal_info": {},
    "experience": [],
    "education": [],
    "skills": []
  }
}
```

---

# 9. Recommended Deployment Architecture

```text
┌────────────────────────────────────────────┐
│                AWS CLOUD                    │
├────────────────────────────────────────────┤
│                                            │
│  Route53                                   │
│      │                                     │
│  Load Balancer                             │
│      │                                     │
│  ECS / Kubernetes Cluster                  │
│      │                                     │
│  FastAPI Containers                        │
│      │                                     │
│  Redis Cluster                             │
│      │                                     │
│  Celery Workers                            │
│      │                                     │
│  PostgreSQL RDS                            │
│      │                                     │
│  Amazon S3                                 │
│      │                                     │
│  Amazon Textract                           │
│      │                                     │
│  OpenAI API                                │
│                                            │
└────────────────────────────────────────────┘
```

---

# 10. Performance Optimization Techniques

| Optimization | Benefit |
|---|---|
| Async Queue Processing | Faster uploads |
| Section-based Parsing | Lower AI cost |
| OCR only when needed | Cost reduction |
| Retry Mechanism | Reliability |
| Caching | Faster repeated operations |
| Multi-worker Architecture | High throughput |
| Batch Processing | Enterprise scalability |

---

# 11. Error Handling Strategy

## Error Recovery Flow

```text
Resume Parsing Failed
        │
        ▼
Automatic Retry
        │
        ▼
Fallback Extraction
        │
        ▼
Human Review Queue
        │
        ▼
Return Partial JSON
```

---

# 12. Final Recommended Technology Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI |
| Queue System | Redis + Celery |
| PDF Parsing | PyMuPDF |
| DOCX Parsing | python-docx |
| OCR | Amazon Textract |
| AI Parsing | OpenAI GPT-4.1 / GPT-4o |
| Validation | Pydantic |
| Database | PostgreSQL |
| File Storage | Amazon S3 |
| Containerization | Docker |
| Deployment | AWS ECS / Kubernetes |
| Monitoring | CloudWatch + Prometheus |

---

# 13. Business Benefits For Client

## Benefits

### High Accuracy
AI-assisted semantic parsing improves resume understanding.

### Scalability
Can process thousands of resumes asynchronously.

### Structured JSON Output
Easy frontend auto-fill and database integration.

### Enterprise Ready
Secure, scalable, fault tolerant architecture.

### Faster Hiring Workflow
Reduces manual candidate data entry.

### Extensible
Can later add:
- Resume scoring
- Candidate-job matching
- AI ranking
- ATS integrations
- Skill intelligence

---

# Final Recommendation

## Recommended Hybrid Architecture

```text
Smart Text Extraction
        +
AI Semantic Parsing
        +
Schema Validation
        +
Normalization Engine
        +
Confidence Scoring
```

This provides the best balance of:
- Accuracy
- Cost Optimization
- Scalability
- Reliability
- Maintainability
- Enterprise Readiness

