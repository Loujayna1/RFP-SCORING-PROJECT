# Traceable AI Scoring of RFP Responses

A retrieval-augmented system that helps procurement committees evaluate vendor responses to Requests for Proposals (RFPs).

In a compliance matrix, each vendor declares its own compliance level for every requirement. The system checks each declaration against the vendor's own proposal. For each requirement, it:

- retrieves the evidence;
- verifies the declared level with a language model;
- reports the level that the evidence actually supports, with the quote and the page it comes from.

Scores and rankings are then computed from the verified levels using the RFP's evaluation matrix. Evaluators keep the final decision: they can review the evidence, override any score with a justification, and every change is recorded in an audit log.

The project was developed as a graduation capstone, following a Design Science Research approach.

---

## Contents

- [Main features](#main-features)
- [How the evaluation works](#how-the-evaluation-works)
- [Architecture](#architecture)
- [Repository structure](#repository-structure)
- [Getting started](#getting-started)
- [Using the application](#using-the-application)
- [Configuration](#configuration)
- [Evaluation scripts](#evaluation-scripts)
- [Tests](#tests)
- [Data and confidentiality](#data-and-confidentiality)
- [Limitations](#limitations)

---

## Main features

- **RFP package ingestion.** An RFP is uploaded as a ZIP archive containing the RFP documents, the compliance matrix, and the evaluation matrix.
  - Structured files (Excel matrices and evaluation grids) are read deterministically.
  - Requirements expressed in narrative documents are extracted with a language model.
- **Knowledge base per RFP.** Documents are converted, split into sections, and indexed as parent and child chunks. Each chunk keeps its document and page.
- **Hybrid retrieval.** Dense embeddings and BM25 are combined in Milvus with Reciprocal Rank Fusion, then reranked by a cross-encoder.
- **Compliance verification.** For each requirement of the compliance matrix, a language model reads the vendor's best five passages. It returns one of the compliance levels of the RFP, with a structured justification and the decisive quote.
  - The application, not the model, locates the quote in the document and resolves its page.
- **Deterministic scoring.** Scores are derived from the verified levels with a fixed scale, then aggregated with the effective weights of the evaluation matrix. The language model never chooses a score.
- **Discrepancy detection.** Requirements whose verified level differs from the declared level are listed, so evaluators can start with possible over-claims.
- **Traceability.** Every total can be broken down into criterion scores, every criterion score into requirement levels, and every level into its quote and page.
- **Human control.**
  - Requirement scores can be overridden with a mandatory reason.
  - Criterion scores can be adjusted within a margin defined for each RFP.
  - The evaluation grid must be approved and locked before scoring.
  - Every change is recorded in the audit log with its author, time, previous and new values, and reason.
- **Conversational assistant.** An assistant answers questions about an RFP and its vendors. It calls tools (document search, scores and rankings, vendor comparison) and cites its sources.
- **Roles and tenants.** Users belong to a tenant and have roles (administrator, technical, financial, operations and legal evaluators, business representative, AI auditor).
- **Reports.** Evaluation reports can be generated as PDF files.

## How the evaluation works

```
RFP package (ZIP)                         Vendor response (ZIP)
  ├─ RFP documents                          ├─ technical proposal (PDF, Word, ...)
  ├─ compliance matrix (Excel)              └─ filled compliance matrix (Excel)
  └─ evaluation matrix (Excel)
          │                                          │
          ▼                                          ▼
  1. Parse and structure                   3. Parse, chunk, and index the proposal
     requirements, criteria, weights          (parent 2,000 / child 400 characters,
          │                                    page kept for every chunk)
          ▼                                          │
  2. Approve and lock the evaluation grid            │
          │                                          ▼
          └──────────────► 4. For each requirement of the compliance matrix:
                              - hybrid search in the vendor's documents (dense + BM25, RRF)
                              - cross-encoder reranking, best 5 passages
                              - language model: verified level + justification + quote
                              - application: locate the quote and its page
                                         │
                                         ▼
                           5. Score from the verified levels, aggregate with the
                              evaluation matrix, rank vendors, list discrepancies
                                         │
                                         ▼
                           6. Review: evidence, overrides with reasons, audit log,
                              conversational assistant, PDF report
```

## Architecture

All services run with Docker Compose.

| Service | Role | Port |
|---|---|---|
| `frontend` | React (Vite, Tailwind) web application | 3000 |
| `backend` | FastAPI application: API, ingestion, evaluation logic | 8000 |
| `worker` | Celery worker: background ingestion and evaluation tasks | — |
| `postgres` | PostgreSQL: RFPs, requirements, scores, users, audit log | 5432 |
| `redis` | Message broker for Celery | 6379 |
| `milvus` | Vector database for dense and sparse (BM25) vectors | 19530 |
| `milvus-etcd`, `milvus-minio` | Storage services required by Milvus | — |
| `milvus-attu` | Web interface to inspect Milvus (optional) | 8080 |
| `app-minio` | Object storage for the uploaded documents | 9000 (API), 9001 (console) |

**Models**

| Purpose | Model | Where it runs |
|---|---|---|
| Verification and assistant | `openai/gpt-oss-120b` | Groq API |
| Classification and query reformulation | `openai/gpt-oss-20b` | Groq API |
| Dense embeddings | `all-MiniLM-L12-v2` | Locally, on CPU |
| Reranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Locally, on CPU |

**Document parsing**

| Format | Library | How it is split |
|---|---|---|
| PDF | Docling, with pdfplumber as a fallback | By section heading and page (Docling), or one section per page (pdfplumber) |
| Word (.docx) | python-docx | Paragraphs grouped into sections at each Heading style |
| Excel (.xlsx, .xlsm) | openpyxl | One section per worksheet, with the sheet name kept |
| Images | Tesseract OCR | Recognized text with word positions |

## Repository structure

```
.
├── docker-compose.yml          # All services
├── backend/
│   ├── Dockerfile
│   ├── entrypoint.sh           # Waits for the database, runs migrations, starts the API
│   ├── requirements.txt
│   ├── alembic/                # Database migrations
│   ├── app/
│   │   ├── main.py             # FastAPI application and main endpoints
│   │   ├── worker.py           # Celery tasks
│   │   ├── core/               # Settings, authentication, Groq key rotation
│   │   ├── models/             # SQLAlchemy models
│   │   ├── routes/             # Human review, evaluators, consensus, tenants
│   │   ├── services/
│   │   │   ├── parser.py                    # Document parsing and sectioning
│   │   │   ├── vector_store.py              # Milvus indexing, parent/child chunks, hybrid search
│   │   │   ├── reranker.py                  # Cross-encoder reranking
│   │   │   ├── evaluation.py                # Evidence retrieval and verification per requirement
│   │   │   ├── llm.py                       # Prompts, scoring calls, conversational agent
│   │   │   ├── matrix_processor.py          # Compliance matrices
│   │   │   ├── evaluation_matrix_parser.py  # Evaluation grids and weights
│   │   │   ├── vendor_matrix.py             # Vendors' returned matrices
│   │   │   ├── criterion_classifier.py      # Mapping of requirements to criteria
│   │   │   └── trace.py                     # Score traces
│   │   └── templates/          # PDF report templates
│   ├── scripts/                # Seeding, evaluation, and maintenance scripts
│   └── tests/                  # Pytest test suite
└── frontend/
    ├── Dockerfile
    └── src/
        ├── pages/              # Dashboard, upload, vendor detail, score editor, audit log, chat, ...
        ├── components/         # RFP wizard, document viewer, chat widget, ...
        └── api/                # API client
```

## Getting started

### Prerequisites

- Docker and Docker Compose.
- At least one Groq API key (<https://console.groq.com>). Several keys can be given; the backend rotates between them and respects their rate limits.
- Enough disk space for the Docker images (Milvus, PostgreSQL, the backend with its document-processing libraries) and the model caches.

### 1. Clone the repository

```bash
git clone <repository-url>
cd <repository-folder>
```

### 2. Create `backend/.env`

`backend/.env` is ignored by Git and must never be committed. Create it with at least:

```env
# Groq API keys, comma-separated
GROQ_API_KEYS=gsk_your_first_key,gsk_your_second_key

# Secret used to sign login tokens: use a long random value
SECRET_KEY=replace-with-a-long-random-string

# Object storage credentials (must match the app-minio service)
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=rfp-documents
```

The database, Redis, and Milvus addresses are set in `docker-compose.yml`. To generate a secret key, run for example `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### 3. Start the services

```bash
docker compose up -d --build
```

The backend waits for PostgreSQL and applies the database migrations automatically. On the first start, the embedding and reranking models are downloaded and cached.

> After changing `backend/.env`, run `docker compose up -d --force-recreate backend worker`. A plain `docker restart` does not reload the environment file.

### 4. Create the first administrator

```bash
docker compose exec \
  -e SEED_ADMIN_EMAIL=you@example.com \
  -e SEED_ADMIN_PASSWORD='choose-a-strong-password' \
  -e SEED_COMPANY='My Company' \
  backend python -m scripts.seed
```

### 5. Open the application

- Web application: <http://localhost:3000>
- API documentation: <http://localhost:8000/docs>

## Using the application

1. **Upload an RFP package** (ZIP) with the RFP documents, the compliance matrix, and the evaluation matrix. You then:
   - check the extracted metadata;
   - define the compliance levels and their ratings;
   - set the margin allowed for manual adjustments of criterion scores.
2. **Review the evaluation grid** (criteria and weights), then approve and lock it. Scoring requires a locked grid.
3. **Upload the vendor responses.** Each response is a ZIP with the technical proposal and the vendor's filled compliance matrix. They are parsed, indexed, and verified in the background.
4. **Review the results.** For each vendor you can see:
   - the total and the criterion scores;
   - the verified level of each requirement next to the declared level;
   - the discrepancies;
   - the justification, the quote, and a link to the page of the evidence.
5. **Correct and decide.**
   - Override requirement scores with a reason, or adjust criterion scores within the allowed margin.
   - Compare vendors.
   - Ask questions in the assistant.
   - Consult the audit log.
   - Export a PDF report.

## Configuration

The main settings are in `backend/app/core/config.py` and can be overridden in `backend/.env`.

| Setting | Default | Meaning |
|---|---|---|
| `GROQ_MAIN_MODEL` | `openai/gpt-oss-120b` | Model used for verification and the assistant |
| `GROQ_FAST_MODEL` | `openai/gpt-oss-20b` | Model used for classification and query reformulation |
| `EVIDENCE_TOP_K` | `5` | Passages given to the model per requirement |
| `RETRIEVAL_MODE` | `hybrid` | `hybrid` (dense + BM25), `dense`, or `sparse` (BM25 only); the last two are for experiments |
| `RERANK_ENABLED` | `true` | Cross-encoder reranking of the retrieved passages |
| `MATRIX_REQUIREMENTS_ONLY` | `true` | Score only the requirements of the compliance matrix |
| `EXTRACT_VENDOR_CLAIMS` | `true` | Extract vendor claims at ingestion; not used for scoring, can be disabled to save tokens |
| `REQUIRE_LOCKED_RUBRIC` | `true` | Require an approved and locked evaluation grid before scoring |
| `GROQ_TPM_PER_KEY` | `7000` | Tokens per minute allowed per key (throttling) |
| `GROQ_MAX_WAIT_SECONDS` | `900` | Maximum wait when all keys are rate-limited |
| `USE_DOCLING` | set in `docker-compose.yml` | Use Docling for PDF conversion; pdfplumber is used otherwise |
| `MAX_UPLOAD_SIZE_MB` | `200` | Maximum upload size |

## Evaluation scripts

The scripts in `backend/scripts/` reproduce the evaluation against a dataset with a known ground truth. Run them from `backend/` while the services are running:

```bash
export RFP_EMAIL=you@example.com RFP_PASSWORD='your-password'

# Evaluate the RFPs of a dataset and compute the metrics
python -m scripts.run_dataset_evaluation run --dataset <path-to-dataset> --tag run1

# Summary of a run (accuracy, macro-F1, Cohen's kappa, timings)
python -m scripts.run_dataset_evaluation summary --dataset <path-to-dataset> --tag run1

# Compare two runs of the same RFP (consistency)
python -m scripts.run_dataset_evaluation consistency --a results/run1/<RFP> --b results/run2/<RFP>

# Time each stage of the pipeline for one RFP
python -m scripts.time_pipeline --dataset <path-to-dataset> --rfp <RFP>
```

The metrics include:

- accuracy, macro-F1, and Cohen's kappa of the verified levels;
- a confusion matrix;
- recall and precision of false-declaration detection;
- evidence page accuracy;
- vendor rankings compared with the expected ranking.

## Tests

```bash
docker compose exec backend pytest
```

## Data and confidentiality

- **Never commit secrets.** `backend/.env` contains API keys and the token signing secret and is ignored by Git. If a key is ever exposed, revoke it in the Groq console.
- **Never commit real procurement data**: RFP packages, vendor responses, database dumps (`*.sql`), or uploaded files.
- **Text sent to an external API.** The verification and the assistant send passages of the documents to the Groq API. Only process documents that you are allowed to send to an external service, or anonymize them first.
  - The embedding and reranking models run locally.
  - The language models are open-weight, so they can be hosted on your own infrastructure for confidential use.

## Limitations

- The system was evaluated on generated RFP packages with a recorded ground truth. Its performance on real, less clearly written proposals has not been measured.
- Partial compliance is the most difficult level to verify.
- The assistant is reliable for questions about one vendor and one topic. It is less reliable for comparisons and ranking explanations, which should be checked against the verified results.
- The review flags based on retrieval scores do not reliably identify incorrect results. The list of discrepancies between declared and verified levels is the recommended starting point for review.
