"""
app/core/config.py

ISSUE #17 FIX: added CORS_ORIGINS to Settings so main.py can read it from
the environment instead of having it hardcoded. Also added missing
MAX_UPLOAD_SIZE_MB and LLM_SEMAPHORE_LIMIT so those values are configurable
without touching source code.
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Groq
    # NOTE: Groq deprecates models periodically. The previous defaults
    # (llama-3.3-70b-versatile / llama-3.1-8b-instant) were fully removed and
    # caused every grade to die with a silent 404. These defaults are the
    # current supported production models; override in .env as Groq's catalogue
    # changes (see https://console.groq.com/docs/models). Startup validation
    # (validate_groq_models) warns loudly when a configured model is unreachable.
    GROQ_API_KEYS:       str   = "gsk_placeholder"
    GROQ_MAIN_MODEL:     str   = "openai/gpt-oss-120b"
    GROQ_FAST_MODEL:     str   = "openai/gpt-oss-20b"

    # Database
    DATABASE_URL:        str   = "postgresql+asyncpg://rfp:rfp_pass@localhost:5432/rfp_evaluator"

    # Redis
    REDIS_URL:           str   = "redis://localhost:6379/0"

    # Milvus vector database
    MILVUS_HOST:         str   = "milvus"
    MILVUS_PORT:         int   = 19530

    # S3 / MinIO
    S3_ENDPOINT:         str   = "http://minio:9000"
    S3_ACCESS_KEY:       str   = "minioadmin"
    S3_SECRET_KEY:       str   = "minioadmin"
    S3_BUCKET:           str   = "rfp-documents"

    # Auth
    SECRET_KEY:          str   = "dev-secret-change-me"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # Pipeline thresholds
    COVERAGE_THRESHOLD:  float = 0.45   # cosine similarity threshold
    LLM_SEMAPHORE_LIMIT: int   = 5      # max concurrent LLM calls
    MAX_UPLOAD_SIZE_MB:  int   = 200    # reject uploads larger than this

    # Verification of vendor claims
    # Where the evidence used to verify a requirement comes from:
    #   "chunks" — passages of the vendor's own documents (verbatim text, real pages)
    #   "claims" — statements extracted from those documents by the LLM
    #   "both"   — the union of the two, reranked together
    # The vendor's filled compliance matrix is never evidence: it is the claim
    # being verified.
    EVIDENCE_SOURCE:     str   = "chunks"
    EVIDENCE_TOP_K:      int   = 5      # passages given to the scorer per requirement
    # Ablation switches (defaults = normal behaviour).
    # RETRIEVAL_MODE: "hybrid" (dense + BM25 fused by RRF), "dense" (embeddings only)
    # or "sparse" (BM25 only).
    RETRIEVAL_MODE:      str   = "hybrid"
    # RERANK_ENABLED: False gives the scorer the top passages in retrieval order,
    # without the cross-encoder (requirement verification only).
    RERANK_ENABLED:      bool  = True
    # When the RFP contains a requirements compliance matrix, only its rows are
    # evaluated; requirements the LLM finds in the narrative documents are kept
    # for reference but not scored (they are mostly submission instructions).
    MATRIX_REQUIREMENTS_ONLY: bool = True
    # Claims are only used for scoring when EVIDENCE_SOURCE == "claims". With
    # "chunks", extracting them costs one LLM call per 5 proposal chunks for
    # nothing the score uses (the assistant can still search the chunks).
    EXTRACT_VENDOR_CLAIMS: bool = True
    # How long a Groq call may wait for a rate-limited key before failing.
    GROQ_MAX_WAIT_SECONDS: int = 900
    # Tokens per minute allowed per key before the client waits (free tier
    # limit is 8,000 for gpt-oss-120b; kept a little under). 0 = no throttle.
    GROQ_TPM_PER_KEY: int = 7000


    # Governance: refuse to grade vendors until the rubric is locked/approved,
    # so scores are never computed against unapproved weights (issue #6). An
    # admin can still override per-submission with acknowledge_unlocked_rubric.
    # Set False to revert to warn-only for teams not yet using rubric approval.
    REQUIRE_LOCKED_RUBRIC: bool = True

    # ISSUE #17 FIX: CORS_ORIGINS was hardcoded in main.py.
    # Set this in .env for staging/production:
    #   CORS_ORIGINS=https://app.yourcompany.com,https://staging.yourcompany.com
    CORS_ORIGINS:        str   = "http://localhost:5173,http://localhost:3000"

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

