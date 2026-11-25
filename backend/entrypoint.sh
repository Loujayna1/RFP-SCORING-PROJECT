#!/bin/bash
# backend/entrypoint.sh
# ─────────────────────────────────────────────────────────────────────────────
# Container entrypoint for the FastAPI backend.
# Runs on both `backend` and `worker` services (worker overrides CMD so only
# the wait logic here applies to it).
#
# Steps:
#   1. Wait for PostgreSQL to accept connections (retry loop — healthcheck alone
#      is not enough because the container may be marked healthy before the DB
#      is fully accepting queries on the application database).
#   2. Run Alembic migrations (idempotent — safe to run on every restart).
#   3. Create the MinIO application bucket if it doesn't exist.
#   4. Start the application (uvicorn for backend, celery for worker — the
#      worker service overrides CMD so step 4 is skipped for it).
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

log() { echo "[entrypoint] $(date -u '+%H:%M:%S') $*"; }

# ── 1. Wait for PostgreSQL ────────────────────────────────────────────────────
log "Waiting for PostgreSQL at postgres:5432 ..."
MAX_RETRIES=30
count=0
until python -c "
import asyncio, asyncpg, os, sys
async def check():
    url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg', 'postgresql')
    try:
        conn = await asyncpg.connect(url)
        await conn.close()
    except Exception as e:
        sys.exit(1)
asyncio.run(check())
" 2>/dev/null; do
  count=$((count + 1))
  if [ "$count" -ge "$MAX_RETRIES" ]; then
    log "ERROR: PostgreSQL not ready after ${MAX_RETRIES} attempts. Aborting."
    exit 1
  fi
  log "  postgres not ready yet (attempt $count/$MAX_RETRIES) — retrying in 3s ..."
  sleep 3
done
log "PostgreSQL is ready."

# ── 2. Run Alembic migrations ─────────────────────────────────────────────────
log "Running Alembic migrations ..."
cd /app
alembic upgrade head
log "Migrations complete."

# ── 3. Create MinIO application bucket ───────────────────────────────────────
log "Ensuring MinIO bucket exists ..."
python - <<'PYEOF'
import boto3, os
from botocore.exceptions import ClientError

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("S3_ENDPOINT", "http://app-minio:9000"),
    aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
    aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
    region_name="us-east-1",
)
bucket = os.environ.get("S3_BUCKET", "rfp-documents")
try:
    s3.head_bucket(Bucket=bucket)
    print(f"[entrypoint] Bucket '{bucket}' already exists.")
except ClientError as e:
    code = e.response["Error"]["Code"]
    if code in ("404", "NoSuchBucket"):
        s3.create_bucket(Bucket=bucket)
        print(f"[entrypoint] Created bucket '{bucket}'.")
    else:
        raise
PYEOF
log "MinIO bucket ready."

# ── 3b. Clear stale Hugging Face incomplete downloads ─────────────────────────
# A hung Docling model download leaves *.incomplete files that block retries.
log "Cleaning stale Hugging Face incomplete downloads (if any) ..."
python - <<'PYEOF'
import glob
import os

for path in glob.glob("/cache/huggingface/hub/**/*.incomplete", recursive=True):
    try:
        os.remove(path)
        print(f"[entrypoint] Removed stale incomplete download: {path}")
    except OSError:
        pass
PYEOF

# ── 4. Start the application ──────────────────────────────────────────────────
# The worker service overrides CMD with `celery ...` so this exec is only
# reached by the backend service.
log "Starting FastAPI application ..."
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --loop asyncio \
  --log-level info
