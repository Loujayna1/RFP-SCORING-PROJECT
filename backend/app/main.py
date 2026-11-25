"""
RFP Evaluator — FastAPI backend
Groq API key rotation + Milvus (all-MiniLM-L12-v2) semantic search + WebSocket RAG chatbot
"""
import asyncio
import json
import logging
import os
import uuid
import zipfile
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from fastapi import FastAPI, File, UploadFile, Form, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import redis.asyncio as redis
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
import boto3

from app.core.config import settings
from app.core.aioutil import loop_semaphore
from app.db.session import AsyncSessionLocal, engine
from app.models import db as models_db
from app.services.parser import parse_document, warmup_docling
from app.services.llm import (
    classify_doc_role,
    extract_requirements,
    extract_vendor_claims,
    extract_cv_data,
    agent_chat_loop,
    build_criteria_from_eval_sheet,
    filter_novel_requirements,
)
from app.core.groq_rotation import get_groq_client
from app.services.vector_store import (
    get_vector_store,
    col_rfp_chunks, col_vendor_chunks,
    col_requirements, col_claims, col_cv_skills,
)
from app.services.evaluation import (
    run_full_evaluation, compute_effective_weights, _select_leaf_criteria, _criterion_code,
)
from app.services.schema_extractor import analyze_rfp_zip_for_schema
from app.services.matrix_processor import extract_requirements_from_xlsx
from app.services.rfp_document_routing import (
    is_compliance_matrix_workbook,
    is_llm_document_source,
    is_evaluation_matrix_workbook,
)
from app.services.trace import build_trace
from app.services.vendor_matrix import read_vendor_declarations
from app.services.evaluation_matrix_parser import (
    parse_evaluation_matrix_xlsx,
    EvaluationMatrixParseError,
)
from sqlalchemy import select, delete, func, or_, and_, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Concurrency limits. Built per running event loop (see app.core.aioutil):
# the Celery worker runs each task in its own asyncio loop, so a process-global
# Semaphore crashes on the second task ("bound to a different event loop").
# Limits are unchanged.
_LLM_SEMAPHORE_LIMIT = 5                                    # prevents rate limits / overload
_VENDOR_PARSE_LIMIT  = int(os.environ.get("VENDOR_PARSE_CONCURRENCY", "2"))  # Docling is CPU-heavy


def LLM_SEMAPHORE() -> asyncio.Semaphore:
    return loop_semaphore("main_llm", _LLM_SEMAPHORE_LIMIT)


def VENDOR_PARSE_SEMAPHORE() -> asyncio.Semaphore:
    return loop_semaphore("main_vendor_parse", _VENDOR_PARSE_LIMIT)
MILVUS_TEXT_MAX_LEN = 32000

# ── ISSUE #1 FIX: removed duplicate `startup` event handler definition.
# The original file defined @app.on_event("startup") twice — once before CORS
# middleware (only initialising FastAPILimiter) and once after (only connecting
# to Milvus).  FastAPI silently runs the LAST registered handler, meaning
# FastAPILimiter was never initialised in production, causing every rate-limited
# endpoint to raise a 500 on first request.
# Fix: merge both startup bodies into a single handler registered after `app` is
# created.

def get_report_worker():
    """
    Import report worker dependencies only when report endpoints need them.
    This keeps the API bootable even if PDF-generation libraries are unavailable.
    """
    try:
        from app.worker import celery_app, generate_report_pdf
        return celery_app, generate_report_pdf
    except Exception as exc:
        logger.error("Report worker dependencies are unavailable: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Report generation is unavailable because worker dependencies failed to load.",
        )

app = FastAPI(title="RFP Evaluator API", version="1.0.0")

# ── ISSUE #2 FIX: CORS origins read from settings, not hardcoded.
# Hardcoded localhost origins break staging/production deployments.
# Added settings.CORS_ORIGINS (comma-separated string, falls back to dev defaults).
_cors_origins = (
    [o.strip() for o in settings.CORS_ORIGINS.split(",")]
    if hasattr(settings, "CORS_ORIGINS") and settings.CORS_ORIGINS
    else ["http://localhost:5173", "http://localhost:3000"]
)
from app.middleware.upload_limit import UploadSizeLimitMiddleware
app.add_middleware(
    UploadSizeLimitMiddleware,
    max_bytes=settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── ISSUE #1 FIX: single merged startup handler ───────────────────────────────
@app.on_event("startup")
async def startup():
    # 1. FastAPILimiter (was missing from effective startup due to duplicate handler)
    r = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    await FastAPILimiter.init(r)

    # 2. Milvus connection with exponential back-off
    vs = get_vector_store()
    for attempt in range(1, 6):
        try:
            await vs.connect()
            break
        except Exception as exc:
            logger.warning("Milvus connect attempt %s/5 failed: %s", attempt, exc)
            if attempt == 5:
                raise
            await asyncio.sleep(min(2 ** attempt, 10))
    # Fail-loud if the app is running with the insecure default signing key.
    # A known SECRET_KEY lets anyone forge admin JWTs, so this must be set in
    # .env for any non-local deployment.
    if settings.SECRET_KEY in ("dev-secret-change-me", "", None):
        logger.error(
            "INSECURE SECRET_KEY — using the built-in default. Anyone can forge "
            "admin tokens. Set a strong random SECRET_KEY in backend/.env "
            "(e.g. `openssl rand -hex 32`) and restart before exposing this app."
        )

    logger.info("Startup complete — Milvus connected, FastAPILimiter initialised.")

    # 2b. Validate the configured Groq models are actually reachable. A
    # deprecated model otherwise fails silently mid-grade with a 404; catch it
    # here so operators see it immediately. Non-fatal — the app still boots.
    try:
        groq = get_groq_client()
        model_check = await groq.validate_models(
            [settings.GROQ_MAIN_MODEL, settings.GROQ_FAST_MODEL]
        )
        if model_check["ok"]:
            logger.info(
                "Groq models OK — main=%s fast=%s",
                settings.GROQ_MAIN_MODEL, settings.GROQ_FAST_MODEL,
            )
        else:
            logger.error(
                "GROQ MODEL CONFIG PROBLEM — grading will fail. Unavailable: %s. "
                "%sSet GROQ_MAIN_MODEL / GROQ_FAST_MODEL in backend/.env to a "
                "supported model (https://console.groq.com/docs/models) and restart.",
                model_check["missing"] or "n/a",
                f"Error: {model_check['error']}. " if model_check["error"] else "",
            )
    except Exception as exc:
        logger.warning("Groq model validation skipped (%s)", exc)

    # 3. Optionally pre-load Docling (skipped when USE_DOCLING=false)
    if os.environ.get("USE_DOCLING", "true").lower() in ("1", "true", "yes"):
        await asyncio.to_thread(warmup_docling)
    else:
        logger.info("USE_DOCLING=false — PDF parsing will use pdfplumber")


DEFAULT_COMPLIANCE_LEVELS = [
    {"label": "Fully Compliant",    "value": 5},
    {"label": "Partially Compliant","value": 3},
    {"label": "Non-Compliant",      "value": 1},
    {"label": "Roadmap Compliant",  "value": 2},
]

# ── Database helpers ──────────────────────────────────────────────────────────
async def get_rfp_details(rfp_id: str):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(models_db.RFPPackage).where(models_db.RFPPackage.rfp_id == rfp_id))
        ).scalar_one_or_none()
        if not row:
            return None
        return {
            "rfp_id": row.rfp_id,
            "title": row.title,
            "issuing_company": row.issuing_company,
            "status": row.status,
            "requirement_count": row.requirement_count,
            "scoring_margin": row.scoring_margin,
            "compliance_levels": row.compliance_levels or [],
            "created_at": row.created_at,
            "rubric_locked": row.rubric_locked_at is not None,
            "rubric_locked_at": row.rubric_locked_at,
            "rubric_approved_by": row.rubric_approved_by,
        }


async def update_rfp_status(rfp_id: str, status: str, **fields):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(models_db.RFPPackage).where(models_db.RFPPackage.rfp_id == rfp_id))
        ).scalar_one_or_none()
        if row:
            row.status = status
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            await db.commit()
        return row


async def get_response_details(response_id: str):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(models_db.VendorResponse).where(models_db.VendorResponse.response_id == response_id))
        ).scalar_one_or_none()
        if not row:
            return None
        return {
            "response_id": row.response_id,
            "rfp_id": row.rfp_id,
            "vendor_id": row.vendor_id,
            "status": row.status,
            "submitted_at": row.submitted_at,
            "created_at": row.created_at,
        }


async def update_response_status(response_id: str, status: str):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(models_db.VendorResponse).where(models_db.VendorResponse.response_id == response_id))
        ).scalar_one_or_none()
        if row:
            row.status = status
            await db.commit()
        return row


async def resolve_tenant_id(entity_type: str, entity_id: str) -> Optional[str]:
    """Look up tenant_id for RFP-scoped entities (used by tasks, txn logs, etc.)."""
    async with AsyncSessionLocal() as db:
        if entity_type == "rfp":
            tid = (
                await db.execute(
                    select(models_db.RFPPackage.tenant_id).where(
                        models_db.RFPPackage.rfp_id == entity_id
                    )
                )
            ).scalar_one_or_none()
        elif entity_type == "vendor_response":
            tid = (
                await db.execute(
                    select(models_db.VendorResponse.tenant_id).where(
                        models_db.VendorResponse.response_id == entity_id
                    )
                )
            ).scalar_one_or_none()
        else:
            tid = None
    return str(tid) if tid else None


async def create_transaction_log(
    entity_type: str,
    entity_id: str,
    operation: str,
    milvus_collections: List[str] = None,
    tenant_id: Optional[str] = None,
):
    if tenant_id is None:
        tenant_id = await resolve_tenant_id(entity_type, entity_id)
    async with AsyncSessionLocal() as db:
        txn = models_db.TransactionLog(
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            milvus_collections=milvus_collections or [],
            status="in_progress",
        )
        db.add(txn)
        await db.commit()
        return txn.txn_id


async def complete_transaction_log(txn_id: str, status: str = "completed", db_tables: List[str] = None):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(models_db.TransactionLog).where(models_db.TransactionLog.txn_id == txn_id))
        ).scalar_one_or_none()
        if row:
            row.status = status
            row.completed_at = datetime.utcnow()
            if db_tables:
                row.db_tables_modified = db_tables
            await db.commit()


async def fail_transaction_log(txn_id: str, error_msg: str):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(models_db.TransactionLog).where(models_db.TransactionLog.txn_id == txn_id))
        ).scalar_one_or_none()
        if row:
            row.status = "failed"
            row.error_message = error_msg
            await db.commit()


async def new_task(
    description: str,
    rfp_id: str = None,
    response_id: str = None,
    tenant_id: Optional[str] = None,
) -> str:
    if tenant_id is None and rfp_id:
        tenant_id = await resolve_tenant_id("rfp", rfp_id)
    elif tenant_id is None and response_id:
        tenant_id = await resolve_tenant_id("vendor_response", response_id)
    tid = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(
            models_db.Task(
                task_id=tid,
                tenant_id=tenant_id,
                description=description,
                status="running",
                progress=0,
                created_at=datetime.utcnow(),
                started_at=datetime.utcnow(),
                rfp_id=rfp_id,
                response_id=response_id,
            )
        )
        await db.commit()
    return tid


async def complete_task(tid: str, result: dict = None):
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(models_db.Task).where(models_db.Task.task_id == tid))).scalar_one_or_none()
        if row:
            row.status = "done"
            row.progress = 100
            row.result = result or {}
            row.completed_at = datetime.utcnow()
            await db.commit()


async def fail_task(tid: str, error: str):
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(models_db.Task).where(models_db.Task.task_id == tid))).scalar_one_or_none()
        if row:
            row.status = "error"
            row.error = error
            row.completed_at = datetime.utcnow()
            await db.commit()


async def update_task_progress(tid: str, progress: int, stage: str = None):
    """Update task progress (0-99) and optional stage label for UI polling."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(models_db.Task).where(models_db.Task.task_id == tid))).scalar_one_or_none()
        if row:
            row.progress = min(99, max(0, progress))
            if stage:
                existing = row.result if isinstance(row.result, dict) else {}
                row.result = {**existing, "stage": stage}
            await db.commit()


async def atomic_db_operation(operation_coro):
    async with AsyncSessionLocal() as db:
        try:
            result = await operation_coro(db)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


# ── Scalar helpers ────────────────────────────────────────────────────────────
def _parse_margin(value, default: int = 10) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _parse_int(value, default: Optional[int] = None, minimum: Optional[int] = None) -> Optional[int]:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        return max(minimum, parsed)
    return parsed


def _parse_compliance_levels(value):
    if not value:
        return list(DEFAULT_COMPLIANCE_LEVELS)
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) and parsed else list(DEFAULT_COMPLIANCE_LEVELS)
    except Exception:
        return list(DEFAULT_COMPLIANCE_LEVELS)


def _safe_int(value, default: int = 5) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not value:
        return default
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return default
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def _parse_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        cleaned = value.strip().replace("Z", "+00:00")
        if not cleaned:
            return None
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(cleaned, fmt)
                except ValueError:
                    continue
    raise ValueError(f"Invalid datetime value: {value}")


def _serialize_datetime(value):
    return value.isoformat() if value else None


def _truncate_for_milvus(text: Optional[str], max_len: int = MILVUS_TEXT_MAX_LEN) -> str:
    if not text:
        return ""
    return text[:max_len]


def _calculate_weighted_total(section_scores: dict, criteria: list):
    total = 0.0
    weight_sum = 0.0
    for criterion in criteria:
        label = criterion.label
        weight = criterion.weight or 0.0
        if label in section_scores:
            total += float(section_scores[label]) * weight
            weight_sum += weight
    if weight_sum > 0:
        return round(total / weight_sum, 4)
    if section_scores:
        return round(sum(float(v) for v in section_scores.values()) / len(section_scores), 4)
    return 0.0


async def record_audit_event(
    db,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    rfp_id: str | None = None,
    tenant_id: str | None = None,
    actor: str = "system",
    actor_user_id: str | None = None,
    details: dict | None = None,
):
    db.add(
        models_db.AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor,
            actor_user_id=actor_user_id,
            details=details or {},
            created_at=datetime.utcnow(),
        )
    )


async def persist_rfp_extraction(
    db,
    *,
    rfp_id: str,
    requirements: list[dict],
    criteria: list[dict],
):
    """Persist extracted RFP requirements/criteria in a single DB transaction."""
    rfp_row = (
        await db.execute(select(models_db.RFPPackage).where(models_db.RFPPackage.rfp_id == rfp_id))
    ).scalar_one_or_none()
    if not rfp_row:
        raise RuntimeError(f"RFP {rfp_id} not found in database")

    await db.execute(delete(models_db.Requirement).where(models_db.Requirement.rfp_id == rfp_id))

    # ── ISSUE #3 FIX: guard criteria re-extraction when rubric is locked.
    # Without this guard, re-running the pipeline on an already-evaluated RFP
    # would silently overwrite weights and types that the procurement officer
    # approved and locked, invalidating all downstream scores.
    if rfp_row.rubric_locked_at is not None:
        logger.info(
            "Rubric for RFP %s is locked (by %s at %s) — skipping criteria re-extraction.",
            rfp_id,
            rfp_row.rubric_approved_by,
            rfp_row.rubric_locked_at.isoformat(),
        )
    else:
        await db.execute(
            delete(models_db.EvaluationCriterion).where(
                models_db.EvaluationCriterion.rfp_id == rfp_id
            )
        )

        async def add_criteria(items: list[dict], parent_uuid: Optional[str] = None):
            for crit in items:
                cid = str(uuid.uuid4())
                db.add(
                    models_db.EvaluationCriterion(
                        criterion_id=cid,
                        rfp_id=rfp_id,
                        label=crit.get("label", ""),
                        section=crit.get("section") or crit.get("label", ""),
                        weight=_safe_float(crit.get("weight"), default=0.0),
                        max_score=_safe_int(crit.get("max_score"), default=5),
                        source_file_hint=crit.get("source_file_hint"),
                        parent_id=parent_uuid,
                        criterion_type=crit.get("criterion_type", "scored"),
                        confidence_score=crit.get("confidence_score"),
                        extraction_source=crit.get("extraction_source"),
                        evaluation_mode=crit.get("evaluation_mode"),
                    )
                )
                if crit.get("children"):
                    await add_criteria(crit["children"], cid)

        await add_criteria(criteria)

    for req in requirements:
        db.add(
            models_db.Requirement(
                req_id=req.get("req_id", str(uuid.uuid4())),
                rfp_id=rfp_id,
                req_code=req.get("req_code", ""),
                description=req.get("description", ""),
                category=req.get("category", ""),
                is_mandatory=req.get("is_mandatory", True),
                source_page=_parse_int(req.get("source_page")),
                source_file=req.get("source_file"),
                source_sheet=req.get("source_sheet"),
                matrix_row_order=req.get("matrix_row_order"),
                matrix_sheet_order=req.get("matrix_sheet_order"),
                req_type=req.get("req_type", "textual"),
                original_text=req.get("original_text"),
                source_doc_id=req.get("source_doc_id"),
            )
        )

    rfp_row.requirement_count = len(requirements)
    rfp_row.status = "ready"
    return rfp_row.status


async def _save_source_docs_to_s3(
    *,
    zf: zipfile.ZipFile,
    file_list: list,
    rfp_id: str,
    response_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> dict:
    """Upload raw files from an open ZipFile to S3 and create Document records.

    Returns a mapping of bare filename → doc_id so callers can embed real
    document UUIDs into Milvus chunk metadata and Requirement.source_doc_id.
    """
    _content_types = {
        "pdf":  "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls":  "application/vnd.ms-excel",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc":  "application/msword",
        "csv":  "text/csv",
    }
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name="us-east-1",
        )
        bucket = settings.S3_BUCKET
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            s3.create_bucket(Bucket=bucket)

        fname_to_doc_id: dict = {}
        doc_records = []

        for name in file_list:
            fname = Path(name).name
            if not fname:
                continue
            data = zf.read(name)
            doc_id = str(uuid.uuid4())
            ext = Path(fname).suffix.lower().lstrip(".")
            key = (
                f"uploads/vendor/{rfp_id}/{response_id}/{fname}"
                if response_id
                else f"uploads/rfp/{rfp_id}/{fname}"
            )
            doc_role = "vendor_response" if response_id else "rfp_document"
            await asyncio.to_thread(
                s3.put_object,
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=_content_types.get(ext, "application/octet-stream"),
            )
            fname_to_doc_id[fname] = doc_id
            doc_records.append(
                models_db.Document(
                    doc_id=doc_id,
                    rfp_id=rfp_id,
                    response_id=response_id,
                    tenant_id=tenant_id,
                    file_name=fname,
                    file_path=f"s3://{bucket}/{key}",
                    file_type=ext,
                    doc_role=doc_role,
                    ocr_status="done",
                )
            )

        if doc_records:
            async with AsyncSessionLocal() as db:
                db.add_all(doc_records)
                await db.commit()

        return fname_to_doc_id
    except Exception as exc:
        logger.warning("_save_source_docs_to_s3 failed (non-fatal): %s", exc)
        return {}


def _attribute_source_pages(reqs: list[dict], batch) -> None:
    """Assign each requirement the page of the chunk its evidence came from.

    The LLM sees several pages at once, so without this every requirement would
    inherit the batch's first chunk page (the old bug where everything landed on
    page 1). We locate each requirement's verbatim ``original_text`` (falling
    back to its description) inside the batch and copy that chunk's real page
    number. The LLM's own ``source_page`` is used only when no text match is
    found.
    """
    default_page = batch[0].page_number if batch else 1
    for r in reqs:
        needle = (r.get("original_text") or r.get("description") or "").strip()
        matched_page = None
        if len(needle) >= 12:
            probe = needle[:60].lower()
            for c in batch:
                if probe in (c.content or "").lower():
                    matched_page = c.page_number
                    break
        if matched_page is not None:
            r["source_page"] = matched_page
        else:
            # Trust the LLM's marker-derived page if it gave a usable one,
            # else fall back to the batch's first page.
            llm_page = _parse_int(r.get("source_page"), minimum=1)
            r["source_page"] = llm_page if llm_page is not None else default_page


async def _dedup_textual_against_matrix(requirements: list[dict]) -> list[dict]:
    """Keep only PDF/Word (textual) requirements NOT already in the Excel matrix.

    The Excel compliance matrix is the authoritative requirement list; the LLM
    document path is meant to surface only requirements the matrix does not
    already contain. Without this, an obligation stated both in the matrix and
    in the RFP prose appears twice. Coverage is judged semantically by the LLM
    (embedding similarity alone cannot separate reworded duplicates from
    genuinely new items here). Matrix rows are never dropped.
    """
    matrix = [r for r in requirements if r.get("req_type") == "matrix"]
    textual = [r for r in requirements if r.get("req_type") != "matrix"]
    if not matrix or not textual:
        return requirements

    try:
        kept = await filter_novel_requirements(textual, matrix)
    except Exception as exc:
        logger.warning("Textual/matrix dedup skipped (%s); keeping all textual", exc)
        return requirements

    return matrix + kept


async def process_rfp_file(*, name: str, zf: zipfile.ZipFile, rfp_id: str, vs, chunks_col: str, doc_id: Optional[str] = None):
    """Process one RFP ZIP entry and return extracted requirement rows."""
    data = zf.read(name)
    fname = Path(name).name
    if not fname:
        return []

    parsed = await asyncio.to_thread(parse_document, data, fname)
    preview = (
        parsed.raw_text[:400]
        if parsed.raw_text
        else (parsed.chunks[0].content[:400] if parsed.chunks else "")
    )
    role = await classify_doc_role(fname, preview)
    logger.info(f"  {fname} -> {role} ({len(parsed.chunks)} chunks)")

    _chunk_doc_id = doc_id or str(uuid.uuid4())
    chunk_items = [
        {
            "id": str(uuid.uuid4()),
            "text": _truncate_for_milvus(c.content),
            "metadata": {
                "rfp_id": rfp_id,
                "vendor_name": "RFP",
                "file_name": fname,
                "page_number": c.page_number,
                "doc_role": role,
                "section": c.section_heading,
                "doc_id": _chunk_doc_id,
            },
        }
        for c in parsed.chunks
    ]
    if chunk_items:
        await asyncio.to_thread(vs.upsert_batch, chunks_col, chunk_items)

    file_reqs: list[dict] = []

    # Dataset A — official compliance matrix only (never evaluation / instruction workbooks)
    if is_compliance_matrix_workbook(fname, role):
        matrix_result = await asyncio.to_thread(extract_requirements_from_xlsx, data, fname)
        matrix_reqs = (
            matrix_result.get("requirements", [])
            if isinstance(matrix_result, dict)
            else matrix_result
        )
        for r in matrix_reqs:
            rid = str(uuid.uuid4())
            r.update({"req_id": rid, "rfp_id": rfp_id, "req_type": "matrix", "source_file": fname})
            file_reqs.append(r)
        logger.info("  %s -> %d matrix requirements (ComplianceMatrix path)", fname, len(matrix_reqs))
    elif fname.lower().endswith((".xlsx", ".xlsm")):
        logger.info("  %s -> skipped matrix extraction (role=%s, not a compliance matrix file)", fname, role)

    # Dataset B — LLM extraction from PDF/DOCX only (never Excel)
    if is_llm_document_source(fname) and parsed.chunks:
        batch_size = 5
        batches = [parsed.chunks[i : i + batch_size] for i in range(0, len(parsed.chunks), batch_size)]

        async def process_batch(batch):
            async with LLM_SEMAPHORE():
                # Label each chunk with its real page so the model can attribute
                # requirements per page instead of all inheriting the first one.
                combined_text = "\n\n".join(
                    f"[PAGE {c.page_number}]\n{c.content}" for c in batch
                )
                reqs = await extract_requirements(combined_text, role, batch[0].page_number)
                _attribute_source_pages(reqs, batch)
                return reqs

        batch_results = await asyncio.gather(*[process_batch(b) for b in batches])
        doc_count = 0
        for sub_list in batch_results:
            for r in sub_list:
                rid = str(uuid.uuid4())
                r.update({"req_id": rid, "rfp_id": rfp_id, "req_type": "textual", "source_file": fname})
                file_reqs.append(r)
                doc_count += 1
        logger.info("  %s -> %d document (LLM) requirements", fname, doc_count)

    return file_reqs


async def process_vendor_file(
    *,
    name: str,
    zf: zipfile.ZipFile,
    rfp_id: str,
    response_id: str,
    vendor_name: str,
    vs,
    vchunks_col: str,
    claims_col: str,
    doc_id: Optional[str] = None,
):
    """
    Process one vendor ZIP entry.

    Returns (team_members, claims, declarations). A returned compliance matrix
    is the vendor's CLAIM, not evidence: its declared levels, responses and
    references are stored as declarations and nothing from it is indexed.
    Indexing it would let each requirement retrieve the vendor's own matrix
    row (which repeats the requirement text) instead of the proposal, so the
    verification would read the claim and not the proof.
    """
    data = zf.read(name)
    fname = Path(name).name
    if not fname:
        return [], [], []

    _chunk_doc_id = doc_id or str(uuid.uuid4())

    if fname.lower().endswith((".xlsx", ".xlsm")):
        declarations = await asyncio.to_thread(read_vendor_declarations, data, fname)
        if declarations:
            logger.info(f"  {fname} -> ComplianceMatrix ({len(declarations)} declarations stored as claims; "
                        f"not indexed as evidence)")
            return [], [], declarations

    parsed = await asyncio.to_thread(parse_document, data, fname)
    preview = (
        parsed.raw_text[:400]
        if parsed.raw_text
        else (parsed.chunks[0].content[:400] if parsed.chunks else "")
    )
    role = await classify_doc_role(fname, preview)
    logger.info(f"  {fname} -> {role} ({len(parsed.chunks)} chunks)")

    file_team = []

    chunk_items = [
        {
            "id": str(uuid.uuid4()),
            "text": _truncate_for_milvus(c.content),
            "metadata": {
                "rfp_id": rfp_id,
                "response_id": response_id,
                "vendor_name": vendor_name,
                "file_name": fname,
                "page_number": c.page_number,
                "doc_role": role,
                "section": c.section_heading,
                "doc_id": _chunk_doc_id,
                "deep_link": f"/docs/{_chunk_doc_id}/view?page={c.page_number}&type=vendor",
            },
        }
        for c in parsed.chunks
    ]
    if chunk_items:
        await asyncio.to_thread(vs.upsert_batch, vchunks_col, chunk_items)

    batch_size = 5
    batches = [parsed.chunks[i:i + batch_size] for i in range(0, len(parsed.chunks), batch_size)]
    is_cv = role == "CV" or "cv" in fname.lower()
    if not settings.EXTRACT_VENDOR_CLAIMS and not is_cv:
        # Scoring reads the proposal chunks (EVIDENCE_SOURCE="chunks"), so
        # claim extraction would spend LLM calls on nothing the score uses.
        batches = []

    async def process_vendor_batch(batch):
        async with LLM_SEMAPHORE():
            batch_text = "\n\n".join([c.content for c in batch])
            start_page = batch[0].page_number
            results = await asyncio.gather(
                extract_cv_data(batch_text, start_page)
                if (role == "CV" or "cv" in fname.lower())
                else asyncio.sleep(0, result=[]),
                extract_vendor_claims(batch_text, role, start_page, batch[0].section_heading)
                if settings.EXTRACT_VENDOR_CLAIMS
                else asyncio.sleep(0, result=[]),
            )
            return results

    batch_results = await asyncio.gather(*[process_vendor_batch(b) for b in batches])

    all_claims = []
    for cv_data, claims in batch_results:
        if cv_data and isinstance(cv_data, dict) and cv_data.get("full_name"):
            cv_data.update(
                {
                    "response_id": response_id,
                    "source_page": 1,
                    "source": fname,
                    "vendor_name": vendor_name,
                }
            )
            file_team.append(cv_data)

        for claim in (claims or []):
            claim_id = str(uuid.uuid4())
            _claim_page = _parse_int(claim.get("source_page"), default=1, minimum=1)
            all_claims.append(
                {
                    "id": claim_id,
                    "text": _truncate_for_milvus(claim.get("claim_text", "")),
                    "metadata": {
                        "rfp_id": rfp_id,
                        "response_id": response_id,
                        "vendor_name": vendor_name,
                        "file_name": fname,
                        "page_number": _claim_page,
                        "claim_type": claim.get("claim_type", "Capability"),
                        "section": claim.get("source_section", ""),
                        "doc_id": _chunk_doc_id,
                        "deep_link": f"/docs/{_chunk_doc_id}/view?page={_claim_page}&type=claim",
                    },
                }
            )

    if all_claims:
        await asyncio.to_thread(vs.upsert_batch, claims_col, all_claims)

    return file_team, all_claims, []


async def persist_vendor_evaluation(
    db,
    *,
    rfp_id: str,
    response_id: str,
    team_members: list[dict],
    all_claims: list[dict],
    evaluation_result: Optional[dict],
    declarations: Optional[list[dict]] = None,
):
    """Persist all vendor evaluation outputs in a single DB transaction."""
    response = (
        await db.execute(
            select(models_db.VendorResponse).where(
                models_db.VendorResponse.response_id == response_id
            )
        )
    ).scalar_one_or_none()
    if not response:
        raise RuntimeError(f"Response {response_id} not found in database")

    await db.execute(delete(models_db.RequirementScore).where(models_db.RequirementScore.response_id == response_id))
    await db.execute(delete(models_db.CoverageEntry).where(models_db.CoverageEntry.response_id == response_id))
    await db.execute(delete(models_db.VendorScore).where(models_db.VendorScore.response_id == response_id))
    await db.execute(delete(models_db.TeamMember).where(models_db.TeamMember.response_id == response_id))
    await db.execute(delete(models_db.VendorDeclaration).where(models_db.VendorDeclaration.response_id == response_id))
    for d in declarations or []:
        db.add(
            models_db.VendorDeclaration(
                declaration_id=str(uuid.uuid4()),
                response_id=response_id,
                req_code=d.get("req_code"),
                source_sheet=d.get("source_sheet"),
                source_row=d.get("source_row"),
                requirement_text=d.get("requirement_text"),
                declared_raw=(d.get("declared_raw") or "")[:200],
                declared_level=d.get("declared_level"),
                response_text=d.get("response_text"),
                reference=d.get("reference"),
                source_file=d.get("source_file"),
            )
        )

    for tm in team_members:
        db.add(
            models_db.TeamMember(
                member_id=str(uuid.uuid4()),
                response_id=response_id,
                full_name=tm.get("full_name"),
                role=tm.get("role"),
                years_experience=_parse_int(tm.get("years_experience"), default=0, minimum=0),
                skills=tm.get("skills", []),
                certifications=tm.get("certifications", []),
                education=tm.get("education", []),
                source_page=_parse_int(tm.get("source_page")),
            )
        )

    await db.execute(delete(models_db.VendorClaim).where(models_db.VendorClaim.response_id == response_id))
    for c in all_claims:
        db.add(
            models_db.VendorClaim(
                claim_id=c["id"],
                response_id=response_id,
                claim_text=c["text"],
                claim_type=c["metadata"].get("claim_type", "Capability"),
                source_page=c["metadata"].get("page_number", 1),
                source_section=c["metadata"].get("section", ""),
            )
        )

    if all_claims:
        await db.flush()

    response.status = "ready"
    response.submitted_at = datetime.utcnow()

    if evaluation_result:
        valid_claim_ids: set[str] = {c["id"] for c in all_claims}

        for coverage_item in evaluation_result.get("coverage", {}).get("entries", []):
            req_id = coverage_item.get("req_id")
            if not req_id:
                continue
            raw_claim_id = coverage_item.get("best_claim_id")
            safe_claim_id = raw_claim_id if raw_claim_id in valid_claim_ids else None
            if raw_claim_id and not safe_claim_id:
                logger.warning(
                    "Dropping stale best_claim_id %s for req %s — likely Milvus eventual-consistency artefact.",
                    raw_claim_id, req_id,
                )
            db.add(
                models_db.CoverageEntry(
                    entry_id=str(uuid.uuid4()),
                    rfp_id=rfp_id,
                    response_id=response_id,
                    req_id=req_id,
                    best_claim_id=safe_claim_id,
                    similarity_score=_safe_float(coverage_item.get("similarity_score"), default=0.0),
                    is_covered=coverage_item.get("is_covered", False),
                    is_gap=coverage_item.get("is_gap", True),
                    evidence_text=coverage_item.get("evidence_text"),
                    evidence_page=_parse_int(coverage_item.get("evidence_page")),
                )
            )

        for req_score in evaluation_result.get("requirement_scores", []):
            req_id       = req_score.get("req_id")
            criterion_id = req_score.get("criterion_id")
            # Allow criterion-level direct scores (req_id=None) as long as
            # criterion_id is present — req_id FK is nullable in the schema.
            if not req_id and not criterion_id:
                continue

            # ── ISSUE #5 FIX: populate ai_score and confidence_flag on creation.
            # Without this, the human review endpoints have no way to know which
            # scores were uncertain, and the original AI score is lost the moment
            # a human overrides it.
            _raw  = _safe_float(req_score.get("raw_score"), default=0.0)
            _sim  = _safe_float(
                (req_score.get("citation") or {}).get("similarity_score"), default=0.0
            )
            _reasoning = req_score.get("reasoning", "")
            # Flag for human review when similarity is low, CoT reasoning is
            # missing, or no evidence was retrieved at all (a coverage gap, not
            # a judged non-compliance — must never pass silently as a hard 0).
            _no_evidence = (
                req_score.get("no_evidence") is True
                or req_score.get("compliance_level") == "NotAddressed"
            )
            _failed = req_score.get("compliance_level") == "ScoringFailed"
            _ev_check = req_score.get("evidence") or {}
            _unverified_quote = bool(_ev_check) and not _ev_check.get("quote_verified", True)
            _flag = (_sim < settings.COVERAGE_THRESHOLD or not _reasoning or _no_evidence
                     or _failed or _unverified_quote)

            db.add(
                models_db.RequirementScore(
                    score_id=str(uuid.uuid4()),
                    req_id=req_id,
                    response_id=response_id,
                    criterion_id=req_score.get("criterion_id"),
                    raw_score=_raw,
                    compliance_level=req_score.get("compliance_level"),
                    justification=req_score.get("justification"),
                    reasoning=_reasoning,
                    scored_at=datetime.utcnow(),
                    ai_score=_raw,
                    confidence_flag=_flag,
                    evidence_quote=(_ev_check.get("quote") or _ev_check.get("passage")) or None,
                    evidence_source=_ev_check.get("source"),
                    evidence_page=_parse_int(_ev_check.get("page")),
                    evidence_link=_ev_check.get("deep_link"),
                )
            )

        vendor_score = evaluation_result.get("vendor_score", {})
        db.add(
            models_db.VendorScore(
                vendor_score_id=str(uuid.uuid4()),
                rfp_id=rfp_id,
                response_id=response_id,
                total_score=vendor_score.get("total_score", 0.0),
                section_scores=vendor_score.get("section_scores", {}),
                computed_at=datetime.utcnow(),
            )
        )

    return response.status


async def cleanup_milvus_collections(vs, collection_names: list[str]):
    for col_name in collection_names:
        try:
            await asyncio.to_thread(vs.drop_collection, col_name)
        except Exception as cleanup_err:
            logger.error("Failed to cleanup Milvus collection %s: %s", col_name, cleanup_err)


async def cleanup_milvus_response_records(vs, collection_names: list[str], response_id: str):
    expr = f'response_id == "{response_id}"'
    for col_name in collection_names:
        try:
            await asyncio.to_thread(vs.delete_by_expression, col_name, expr)
        except Exception as cleanup_err:
            logger.error(
                "Failed to cleanup Milvus records in %s for response %s: %s",
                col_name, response_id, cleanup_err,
            )


class ReportRequest(BaseModel):
    report_type: str
    format:      str
    vendor_ids:  list[str] = []


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND PIPELINES
# ══════════════════════════════════════════════════════════════════════════════

async def pipeline_rfp(rfp_id: str, file_bytes: bytes, task_id: str, confirmed_criteria: Optional[List[Dict]] = None):
    chunks_col = col_rfp_chunks(rfp_id)
    reqs_col   = col_requirements(rfp_id)
    milvus_collections = [chunks_col, reqs_col]
    vs = get_vector_store()

    try:
        rfp_row = await get_rfp_details(rfp_id)
        if not rfp_row:
            raise ValueError(f"RFP {rfp_id} not found in database")

        requirements: list[dict] = []
        criteria:     list[dict] = []

        txn_id = await create_transaction_log("rfp", rfp_id, "ingest_rfp", milvus_collections=milvus_collections)

        if confirmed_criteria:
            criteria = confirmed_criteria

        tenant_id_str = await resolve_tenant_id("rfp", rfp_id)

        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            file_list = [n for n in zf.namelist() if not n.endswith("/") and not n.startswith("__")]
            logger.info(f"RFP ZIP contains {len(file_list)} files")

            fname_to_doc_id = await _save_source_docs_to_s3(
                zf=zf,
                file_list=file_list,
                rfp_id=rfp_id,
                tenant_id=tenant_id_str,
            )

            # ── Evaluation matrix detection ────────────────────────────────────
            # If any file has 'evaluation' in its name (e.g. evaluation_matrix.xlsx),
            # parse it as a criteria tree instead of as requirements.
            # confirmed_criteria from the UI always take priority; the matrix
            # file is only used when no criteria have been confirmed yet.
            if not criteria:
                for name in file_list:
                    fname = Path(name).name
                    if is_evaluation_matrix_workbook(fname):
                        try:
                            data = zf.read(name)
                            eval_criteria_tree = parse_evaluation_matrix_xlsx(data, fname)
                            criteria = eval_criteria_tree
                            logger.info(
                                "Loaded evaluation matrix from '%s': %d top-level criteria nodes",
                                fname, len(criteria),
                            )
                            break   # use the first valid eval matrix found
                        except EvaluationMatrixParseError as exc:
                            # Fail loudly — do NOT silently fall back to the default rubric.
                            # The user uploaded an evaluation matrix that cannot be parsed;
                            # proceeding with the default rubric would produce wrong scores.
                            logger.error(
                                "Evaluation matrix '%s' could not be parsed: %s. "
                                "RFP pipeline aborted — fix the file and re-upload.",
                                fname, exc,
                            )
                            raise RuntimeError(
                                f"Evaluation matrix '{fname}' could not be parsed: {exc}"
                            ) from exc

            # ── Requirement + chunk extraction ─────────────────────────────────
            results = await asyncio.gather(
                *[
                    process_rfp_file(
                        name=n, zf=zf, rfp_id=rfp_id, vs=vs, chunks_col=chunks_col,
                        doc_id=fname_to_doc_id.get(Path(n).name),
                    )
                    for n in file_list
                ]
            )
            for sub_list in results:
                requirements.extend(sub_list)

        # Drop textual (PDF/Word) requirements already covered by the Excel
        # matrix so the Document tab shows only net-new items (no redundancy).
        requirements = await _dedup_textual_against_matrix(requirements)

        for req in requirements:
            src_fname = req.get("source_file")
            if src_fname and src_fname in fname_to_doc_id:
                req["source_doc_id"] = fname_to_doc_id[src_fname]

        if requirements:
            req_items = [{
                "id": r["req_id"], "text": _truncate_for_milvus(r["description"]),
                "metadata": {
                    "rfp_id": rfp_id, "file_name": r.get("source_file", ""),
                    "req_code": r.get("req_code", ""), "req_type": r.get("req_type", "textual")
                }
            } for r in requirements]
            await asyncio.to_thread(vs.upsert_batch, reqs_col, req_items)

        if not criteria:
            # No evaluation matrix file and no confirmed criteria — use legacy defaults.
            criteria = [
                {"label": "Functional",     "section": "Functional",     "weight": 0.35, "max_score": 5},
                {"label": "Technical",      "section": "Technical",      "weight": 0.35, "max_score": 5},
                {"label": "Security",       "section": "Security",       "weight": 0.10, "max_score": 5},
                {"label": "References",     "section": "References",     "weight": 0.15, "max_score": 5},
                {"label": "Infrastructure", "section": "Infrastructure", "weight": 0.05, "max_score": 5},
            ]

        status = await atomic_db_operation(
            lambda db: persist_rfp_extraction(db, rfp_id=rfp_id, requirements=requirements, criteria=criteria)
        )

        await update_rfp_status(rfp_id, status, requirement_count=len(requirements))
        await complete_task(task_id, {
            "rfp_id": rfp_id,
            "requirement_count": len(requirements),
            "chunks_indexed": vs.collection_count(chunks_col),
        })
        await complete_transaction_log(txn_id, db_tables=["rfp_package", "requirements", "evaluation_criteria"])
        logger.info(f"RFP {rfp_id} ready: {len(requirements)} requirements")

    except Exception as e:
        logger.error(f"RFP pipeline failed: {e}", exc_info=True)
        await update_rfp_status(rfp_id, "error")
        if "txn_id" in locals():
            await fail_transaction_log(txn_id, str(e))
        await cleanup_milvus_collections(vs, milvus_collections)
        await fail_task(task_id, str(e))


async def pipeline_vendor(rfp_id: str, response_id: str, vendor_name: str, file_bytes: bytes, task_id: str):
    vchunks_col  = col_vendor_chunks(rfp_id)
    claims_col   = col_claims(rfp_id)
    cv_col       = col_cv_skills(rfp_id)
    milvus_collections = [vchunks_col, claims_col, cv_col]
    vs = get_vector_store()

    expr = f'response_id == "{response_id}"'
    for col_name in milvus_collections:
        try:
            await asyncio.to_thread(vs.delete_by_expression, col_name, expr)
        except Exception as cleanup_err:
            logger.warning(f"Failed to pre-cleanup Milvus collection {col_name}: {cleanup_err}")

    try:
        async with AsyncSessionLocal() as db:
            requirement_rows = (
                await db.execute(select(models_db.Requirement).where(models_db.Requirement.rfp_id == rfp_id))
            ).scalars().all()
            criteria_rows = (
                await db.execute(
                    select(models_db.EvaluationCriterion).where(models_db.EvaluationCriterion.rfp_id == rfp_id)
                )
            ).scalars().all()
            rfp_compliance_levels = (
                await db.execute(
                    select(models_db.RFPPackage.compliance_levels).where(models_db.RFPPackage.rfp_id == rfp_id)
                )
            ).scalar_one_or_none() or list(DEFAULT_COMPLIANCE_LEVELS)

        requirements = [
            {
                "req_id": str(req.req_id),
                "req_code": req.req_code or "",
                "description": req.description or "",
                "category": req.category or "General",
                "is_mandatory": bool(req.is_mandatory),
                "source_page": req.source_page,
                "source_sheet": req.source_sheet,
                "req_type": req.req_type or "textual",
            }
            for req in requirement_rows
            if req.description and not _is_matrix_heading_row(req)
        ]
        # The compliance matrix is the authoritative list of what is evaluated.
        # Sentences the LLM extracted from the narrative RFP documents ("each
        # respondent shall submit two documents…") are instructions to
        # bidders, not capabilities to verify.
        if settings.MATRIX_REQUIREMENTS_ONLY and any(r["req_type"] == "matrix" for r in requirements):
            skipped = sum(1 for r in requirements if r["req_type"] != "matrix")
            requirements = [r for r in requirements if r["req_type"] == "matrix"]
            if skipped:
                logger.info("Evaluating %d matrix requirements; %d narrative requirements not scored",
                            len(requirements), skipped)
        criteria = [
            {
                "criterion_id":   str(crit.criterion_id) if crit.criterion_id else None,
                "parent_id":      str(crit.parent_id) if crit.parent_id else None,
                "label":          crit.label or "",
                "section":        crit.section or crit.label or "",
                "weight":         _safe_float(crit.weight, default=0.0),
                "max_score":      _safe_int(crit.max_score, default=5),
                "criterion_type": crit.criterion_type or "scored",
                "evaluation_mode": crit.evaluation_mode,
            }
            for crit in criteria_rows
        ]

        txn_id = await create_transaction_log("vendor_response", response_id, "ingest_vendor", milvus_collections=milvus_collections)
        team_members: list[dict] = []
        vendor_claims: list[dict] = []
        vendor_declarations: list[dict] = []
        evaluation_result = None
        tenant_id_str = await resolve_tenant_id("vendor_response", response_id)

        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            file_list = [n for n in zf.namelist() if not n.endswith("/") and not n.startswith("__")]
            total_files = len(file_list)
            await update_task_progress(task_id, 5, f"parsing 0/{total_files} files")

            fname_to_doc_id = await _save_source_docs_to_s3(
                zf=zf,
                file_list=file_list,
                rfp_id=rfp_id,
                response_id=response_id,
                tenant_id=tenant_id_str,
            )

            progress_lock = asyncio.Lock()
            completed_files = 0

            async def process_one_vendor_file(name: str):
                nonlocal completed_files
                async with VENDOR_PARSE_SEMAPHORE():
                    result = await process_vendor_file(
                        name=name, zf=zf, rfp_id=rfp_id, response_id=response_id,
                        vendor_name=vendor_name, vs=vs, vchunks_col=vchunks_col, claims_col=claims_col,
                        doc_id=fname_to_doc_id.get(Path(name).name),
                    )
                async with progress_lock:
                    completed_files += 1
                    pct = 5 + int(70 * completed_files / max(total_files, 1))
                    await update_task_progress(
                        task_id, pct, f"parsing {completed_files}/{total_files} files",
                    )
                return result

            results = await asyncio.gather(*[process_one_vendor_file(n) for n in file_list])
            for sub_team, sub_claims, sub_declarations in results:
                team_members.extend(sub_team)
                vendor_claims.extend(sub_claims)
                vendor_declarations.extend(sub_declarations)

        await update_task_progress(task_id, 80, "scoring")
        await update_response_status(response_id, "scoring")
        logger.info(
            f"Vendor {vendor_name} indexed: "
            f"{vs.collection_count(claims_col)} claims, "
            f"{vs.collection_count(cv_col)} CV entries, "
            f"{vs.collection_count(vchunks_col)} chunks"
        )

        if requirements:
            evaluation_result = await run_full_evaluation(
                rfp_id, response_id, requirements, criteria,
                compliance_levels=rfp_compliance_levels,
            )

        status = await atomic_db_operation(
            lambda db: persist_vendor_evaluation(
                db, rfp_id=rfp_id, response_id=response_id,
                team_members=team_members, all_claims=vendor_claims,
                evaluation_result=evaluation_result,
                declarations=vendor_declarations,
            )
        )

        await update_response_status(response_id, status)

        if evaluation_result:
            await complete_task(task_id, {
                "response_id": response_id,
                "total_score": evaluation_result["vendor_score"]["total_score"],
                "coverage_rate": evaluation_result["coverage"]["coverage_rate"],
            })
        else:
            await complete_task(task_id, {"response_id": response_id, "warning": "No RFP requirements to evaluate against"})

        await complete_transaction_log(
            txn_id,
            db_tables=["vendor_response", "team_members", "coverage_entries", "requirement_scores", "vendor_scores"],
        )

    except Exception as e:
        logger.error(f"Vendor pipeline failed: {e}", exc_info=True)
        await update_response_status(response_id, "error")
        if "txn_id" in locals():
            await fail_transaction_log(txn_id, str(e))
        await cleanup_milvus_response_records(vs, milvus_collections, response_id)
        await fail_task(task_id, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ── ISSUE #6 FIX: register human_review router.
from app.routes.human_review  import router as human_review_router
from app.routes.tenant        import router as tenant_router
from app.routes.evaluator     import router as evaluator_router
from app.routes.consensus     import router as consensus_router
from app.middleware.tenant_middleware import TenantMiddleware
from app.core.auth            import router as auth_router_new, require_role, get_current_user, decode_token

app.include_router(human_review_router)
app.include_router(tenant_router)
app.include_router(evaluator_router)
app.include_router(consensus_router)
app.include_router(auth_router_new)
app.add_middleware(TenantMiddleware)


@app.get("/api/v1/health")
async def health(check_llm: bool = False):
    vs = get_vector_store()
    body = {
        "status":      "ok",
        "groq_keys":   len(settings.GROQ_API_KEYS.split(",")),
        "milvus":      vs._connected,
        "embed_model": "all-MiniLM-L12-v2",
        "groq_main_model": settings.GROQ_MAIN_MODEL,
        "groq_fast_model": settings.GROQ_FAST_MODEL,
    }
    # Opt-in live check (one Groq /models call) so monitoring can detect a
    # deprecated model before a grade does. Off by default to keep /health cheap.
    if check_llm:
        try:
            check = await get_groq_client().validate_models(
                [settings.GROQ_MAIN_MODEL, settings.GROQ_FAST_MODEL]
            )
            body["llm_models_ok"] = check["ok"]
            body["llm_models_missing"] = check["missing"]
            if not check["ok"]:
                body["status"] = "degraded"
        except Exception as exc:
            body["llm_models_ok"] = False
            body["llm_error"] = str(exc)
            body["status"] = "degraded"
    return body


@app.get("/api/v1/db/pool-status")
async def db_pool_status(payload: dict = Depends(require_role("admin"))):
    pool = engine.pool
    return {
        "size":       pool.size(),
        "checked_in": pool.checkedin(),
        "overflow":   pool.overflow(),
        "checked_out": pool.checkedout(),
    }


# ── RFP package ───────────────────────────────────────────────────────────────
@app.post("/api/v1/rfp/analyze", dependencies=[Depends(RateLimiter(times=5, seconds=60))])
async def analyze_rfp(file: UploadFile = File(...), payload: dict = Depends(require_role("admin"))):
    content = await file.read()
    schema = await asyncio.to_thread(analyze_rfp_zip_for_schema, content)
    return {"schema": schema}


@app.post("/api/v1/rfp", dependencies=[Depends(RateLimiter(times=3, seconds=60))])
async def create_rfp(
    background_tasks: BackgroundTasks,
    file:               UploadFile    = File(...),
    title:              str           = Form(...),
    issuing_company:    str           = Form(...),
    issuing_date:       Optional[str] = Form(None),
    scoring_margin:     Optional[str] = Form(None),
    compliance_levels:  Optional[str] = Form(None),
    submission_deadline:Optional[str] = Form(None),
    confirmed_criteria: Optional[str] = Form(None),
    payload:            dict          = Depends(require_role("admin")),
):
    rfp_id = str(uuid.uuid4())
    content = await file.read()
    parsed_margin   = _parse_margin(scoring_margin)
    parsed_levels   = _parse_compliance_levels(compliance_levels)
    parsed_criteria = None
    if confirmed_criteria:
        try:
            parsed_criteria = json.loads(confirmed_criteria)
        except Exception:
            logger.warning("Failed to parse confirmed_criteria JSON")
    try:
        parsed_deadline = _parse_datetime(submission_deadline)
    except ValueError:
        parsed_deadline = None

    parsed_issuing_date = None
    if issuing_date:
        try:
            parsed_issuing_date = _parse_datetime(issuing_date)
        except Exception:
            logger.warning("Failed to parse issuing_date")

    tenant_id = payload["tenant_id"]
    user_id   = payload["sub"]

    async with AsyncSessionLocal() as db:
        rfp_row = models_db.RFPPackage(
            rfp_id=rfp_id,
            tenant_id=tenant_id,
            title=title,
            issuing_company=issuing_company,
            issuing_date=parsed_issuing_date,
            rfp_reference=None,
            submission_deadline=parsed_deadline,
            status="ingesting",
            requirement_count=0,
            scoring_margin=parsed_margin,
            compliance_levels=parsed_levels,
            created_at=datetime.utcnow(),
            created_by=user_id,
        )
        db.add(rfp_row)
        await db.flush()
        await record_audit_event(
            db,
            action="create_rfp",
            entity_type="rfp_package",
            entity_id=rfp_id,
            rfp_id=rfp_id,
            tenant_id=tenant_id,
            actor=payload.get("email", "admin"),
            actor_user_id=user_id,
            details={
                "title": title,
                "issuing_company": issuing_company,
                "scoring_margin": parsed_margin,
                "compliance_levels": parsed_levels,
            },
        )
        await db.commit()

    task_id = await new_task(f"Process RFP: {title}", rfp_id=rfp_id, tenant_id=tenant_id)
    background_tasks.add_task(pipeline_rfp, rfp_id, content, task_id, parsed_criteria)
    return {"rfp_id": rfp_id, "status": "ingesting", "task_id": task_id, "estimated_seconds": 60}


@app.get("/api/v1/rfp")
async def list_rfps(payload: dict = Depends(get_current_user)):
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        rfps = (
            await db.execute(
                select(models_db.RFPPackage)
                .where(models_db.RFPPackage.tenant_id == tenant_id)
                .order_by(models_db.RFPPackage.created_at.desc())
            )
        ).scalars().all()

        vendor_counts = {
            str(rfp_key): count
            for rfp_key, count in (
                await db.execute(
                    select(models_db.VendorResponse.rfp_id, func.count(models_db.VendorResponse.response_id))
                    .where(models_db.VendorResponse.status != "error")
                    .group_by(models_db.VendorResponse.rfp_id)
                )
            ).all()
        }

        return [
            {
                "rfp_id": str(rfp.rfp_id),
                "title": rfp.title,
                "issuing_company": rfp.issuing_company,
                "submission_deadline": rfp.submission_deadline.isoformat() if rfp.submission_deadline else None,
                "status": rfp.status,
                "requirement_count": rfp.requirement_count or 0,
                "scoring_margin": rfp.scoring_margin if rfp.scoring_margin is not None else 10,
                "compliance_levels": rfp.compliance_levels if rfp.compliance_levels else list(DEFAULT_COMPLIANCE_LEVELS),
                "vendor_count": vendor_counts.get(str(rfp.rfp_id), 0),
                "created_at": rfp.created_at.isoformat() if rfp.created_at else None,
                # ISSUE #7 FIX: expose rubric lock state so UI can show the lock badge
                "rubric_locked": rfp.rubric_locked_at is not None,
            }
            for rfp in rfps
        ]


@app.delete("/api/v1/rfp/{rfp_id}")
async def delete_rfp(rfp_id: str, payload: dict = Depends(require_role("admin"))):
    vs = get_vector_store()

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])

        resp_rows = (
            await db.execute(select(models_db.VendorResponse.response_id).where(models_db.VendorResponse.rfp_id == rfp_id))
        ).scalars().all()

        await db.execute(delete(models_db.Task).where(models_db.Task.rfp_id == rfp_id))
        if resp_rows:
            await db.execute(delete(models_db.Task).where(models_db.Task.response_id.in_(resp_rows)))
            await db.execute(delete(models_db.RequirementScore).where(models_db.RequirementScore.response_id.in_(resp_rows)))
            await db.execute(delete(models_db.TeamMember).where(models_db.TeamMember.response_id.in_(resp_rows)))
            await db.execute(delete(models_db.VendorDeclaration).where(models_db.VendorDeclaration.response_id.in_(resp_rows)))
            await db.execute(delete(models_db.VendorClaim).where(models_db.VendorClaim.response_id.in_(resp_rows)))

        await db.execute(delete(models_db.CoverageEntry).where(models_db.CoverageEntry.rfp_id == rfp_id))
        await db.execute(delete(models_db.VendorScore).where(models_db.VendorScore.rfp_id == rfp_id))
        await db.execute(delete(models_db.Report).where(models_db.Report.rfp_id == rfp_id))
        await db.execute(delete(models_db.Requirement).where(models_db.Requirement.rfp_id == rfp_id))
        await db.execute(delete(models_db.EvaluationCriterion).where(models_db.EvaluationCriterion.rfp_id == rfp_id))

        doc_query = select(models_db.Document.doc_id).where(models_db.Document.rfp_id == rfp_id)
        if resp_rows:
            doc_query = doc_query.union_all(
                select(models_db.Document.doc_id).where(models_db.Document.response_id.in_(resp_rows))
            )
        doc_ids = (await db.execute(doc_query)).scalars().all()
        if doc_ids:
            await db.execute(delete(models_db.OCRResult).where(models_db.OCRResult.doc_id.in_(doc_ids)))
            await db.execute(delete(models_db.DocChunk).where(models_db.DocChunk.doc_id.in_(doc_ids)))
            await db.execute(delete(models_db.Document).where(models_db.Document.doc_id.in_(doc_ids)))

        await db.execute(delete(models_db.VendorResponse).where(models_db.VendorResponse.rfp_id == rfp_id))
        # audit_log is intentionally immutable by trigger, but deleting an RFP
        # requires removing its audit rows first because audit_log.rfp_id has a
        # restrictive FK. Disable the trigger only for this transaction so the
        # cleanup can proceed, then restore it before commit.
        await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER trig_audit_log_immutable"))
        try:
            await db.execute(delete(models_db.AuditLog).where(models_db.AuditLog.rfp_id == rfp_id))
        finally:
            await db.execute(text("ALTER TABLE audit_log ENABLE TRIGGER trig_audit_log_immutable"))

        session_ids = (
            await db.execute(select(models_db.ChatSession.session_id).where(models_db.ChatSession.rfp_id == rfp_id))
        ).scalars().all()
        if session_ids:
            await db.execute(delete(models_db.ChatMessage).where(models_db.ChatMessage.session_id.in_(session_ids)))
            await db.execute(delete(models_db.ChatSession).where(models_db.ChatSession.session_id.in_(session_ids)))

        await db.execute(delete(models_db.RFPPackage).where(models_db.RFPPackage.rfp_id == rfp_id))
        await db.commit()

    try:
        await asyncio.to_thread(vs.drop_collection, col_rfp_chunks(rfp_id))
        await asyncio.to_thread(vs.drop_collection, col_requirements(rfp_id))
        await asyncio.to_thread(vs.drop_collection, col_vendor_chunks(rfp_id))
        await asyncio.to_thread(vs.drop_collection, col_claims(rfp_id))
        await asyncio.to_thread(vs.drop_collection, col_cv_skills(rfp_id))
    except Exception as exc:
        logger.warning("Failed to drop Milvus collections for RFP %s: %s", rfp_id, exc)

    return {"deleted": True, "rfp_id": rfp_id}


@app.delete("/api/v1/rfp/{rfp_id}/vendor/{response_id}")
async def delete_vendor_response(rfp_id: str, response_id: str, payload: dict = Depends(require_role("admin"))):
    vs = get_vector_store()
    async with AsyncSessionLocal() as db:
        await _get_response_for_tenant(db, rfp_id, response_id, payload["tenant_id"])
        await db.execute(delete(models_db.RequirementScore).where(models_db.RequirementScore.response_id == response_id))
        await db.execute(delete(models_db.CoverageEntry).where(models_db.CoverageEntry.response_id == response_id))
        await db.execute(delete(models_db.VendorClaim).where(models_db.VendorClaim.response_id == response_id))
        await db.execute(delete(models_db.TeamMember).where(models_db.TeamMember.response_id == response_id))
        await db.execute(delete(models_db.VendorDeclaration).where(models_db.VendorDeclaration.response_id == response_id))
        await db.execute(delete(models_db.VendorScore).where(models_db.VendorScore.response_id == response_id))

        doc_ids = (
            await db.execute(select(models_db.Document.doc_id).where(models_db.Document.response_id == response_id))
        ).scalars().all()
        if doc_ids:
            await db.execute(delete(models_db.OCRResult).where(models_db.OCRResult.doc_id.in_(doc_ids)))
            await db.execute(delete(models_db.DocChunk).where(models_db.DocChunk.doc_id.in_(doc_ids)))
            await db.execute(delete(models_db.Document).where(models_db.Document.doc_id.in_(doc_ids)))

        await db.execute(delete(models_db.VendorResponse).where(models_db.VendorResponse.response_id == response_id))
        await db.commit()

    expr = f'response_id == "{response_id}"'
    try:
        await asyncio.to_thread(vs.delete_by_expression, col_vendor_chunks(rfp_id), expr)
        await asyncio.to_thread(vs.delete_by_expression, col_claims(rfp_id), expr)
        await asyncio.to_thread(vs.delete_by_expression, col_cv_skills(rfp_id), expr)
    except Exception as exc:
        logger.warning("Failed to delete Milvus records for response %s: %s", response_id, exc)

    return {"deleted": True, "response_id": response_id}


@app.get("/api/v1/rfp/{rfp_id}")
async def get_rfp(rfp_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])

        vendor_count = (
            await db.execute(
                select(func.count(models_db.VendorResponse.response_id)).where(
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.status != "error",
                )
            )
        ).scalar_one()

        return {
            "rfp_id": str(rfp.rfp_id),
            "title": rfp.title,
            "issuing_company": rfp.issuing_company,
            "submission_deadline": rfp.submission_deadline.isoformat() if rfp.submission_deadline else None,
            "status": rfp.status,
            "requirement_count": rfp.requirement_count or 0,
            "scoring_margin": rfp.scoring_margin if rfp.scoring_margin is not None else 10,
            "compliance_levels": rfp.compliance_levels if rfp.compliance_levels else list(DEFAULT_COMPLIANCE_LEVELS),
            "vendor_count": vendor_count,
            "created_at": rfp.created_at.isoformat() if rfp.created_at else None,
            "rubric_locked": rfp.rubric_locked_at is not None,
            "rubric_locked_at": rfp.rubric_locked_at.isoformat() if rfp.rubric_locked_at else None,
            "rubric_approved_by": rfp.rubric_approved_by,
        }


def _serialize_requirement_row(r: models_db.Requirement) -> dict:
    return {
        "req_id": str(r.req_id),
        "req_code": r.req_code,
        "description": r.description,
        "category": r.category,
        "is_mandatory": r.is_mandatory,
        "source_page": r.source_page,
        "original_text": r.original_text,
        "file_name": r.source_file,
        "source_doc_id": str(r.source_doc_id) if r.source_doc_id else None,
        "source_sheet": r.source_sheet,
        "matrix_row_order": r.matrix_row_order,
        "matrix_sheet_order": r.matrix_sheet_order,
        "req_type": r.req_type or "textual",
    }


def _is_matrix_heading_row(r: models_db.Requirement) -> bool:
    """Filter out worksheet titles and instruction lines that were persisted as rows."""
    text = (r.description or r.original_text or "").strip().lower()
    if not text:
        return True
    category = (r.source_sheet or r.category or "").strip().lower()
    if text in {"instructions", "instruction"}:
        return True
    if text.startswith("the vendor shall only use these compliance statements"):
        return True
    if text.startswith("all items having such compliance statements"):
        return True
    if text.startswith("the vendor is invited to provide"):
        return True
    if category and text == category:
        return True
    if category and text == f"{category} requirements":
        return True
    if text.endswith(" requirements") and len(text.split()) <= 3:
        return True
    return False


async def _get_response_for_tenant(db, rfp_id: str, response_id: str, tenant_id):
    resp = (
        await db.execute(
            select(models_db.VendorResponse).where(
                models_db.VendorResponse.response_id == response_id,
                models_db.VendorResponse.rfp_id == rfp_id,
                models_db.VendorResponse.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not resp:
        raise HTTPException(404, "Response not found")
    return resp


async def _get_rfp_for_tenant(db, rfp_id: str, tenant_uuid: uuid.UUID):
    rfp = (
        await db.execute(
            select(models_db.RFPPackage).where(
                models_db.RFPPackage.rfp_id == rfp_id,
                models_db.RFPPackage.tenant_id == tenant_uuid,
            )
        )
    ).scalar_one_or_none()
    if not rfp:
        raise HTTPException(404, "RFP not found")
    return rfp


@app.get("/api/v1/rfp/{rfp_id}/requirements/matrix")
async def get_matrix_requirements_grouped(
    rfp_id: str,
    payload: dict = Depends(require_role()),
):
    """
    Official matrix requirements (Dataset A): deterministic Excel extraction,
    grouped by worksheet. Each section = one category (sheet name).
    """
    tenant_uuid = uuid.UUID(payload["tenant_id"])
    async with AsyncSessionLocal() as db:
        await _get_rfp_for_tenant(db, rfp_id, tenant_uuid)
        rows = (
            await db.execute(
                select(models_db.Requirement)
                .where(
                    models_db.Requirement.rfp_id == rfp_id,
                    models_db.Requirement.req_type == "matrix",
                )
                .order_by(
                    models_db.Requirement.matrix_sheet_order.asc().nullsfirst(),
                    models_db.Requirement.source_sheet.asc().nullsfirst(),
                    models_db.Requirement.matrix_row_order.asc().nullsfirst(),
                    models_db.Requirement.req_code.asc().nullslast(),
                )
            )
        ).scalars().all()

        sections_map: dict[str, dict] = {}
        section_order: list[str] = []
        for r in rows:
            if _is_matrix_heading_row(r):
                continue
            key = r.source_sheet or r.category or "Uncategorized"
            if key not in sections_map:
                sections_map[key] = {
                    "sheet_name": r.source_sheet or key,
                    "category": r.category or key,
                    "sheet_index": r.matrix_sheet_order if r.matrix_sheet_order is not None else 999,
                    "count": 0,
                    "requirements": [],
                }
                section_order.append(key)
            sec = sections_map[key]
            sec["requirements"].append(_serialize_requirement_row(r))
            sec["count"] = len(sec["requirements"])

        sections = sorted(
            [sections_map[k] for k in section_order],
            key=lambda s: (s["sheet_index"], s["sheet_name"]),
        )
        total = sum(s["count"] for s in sections)

        return {
            "rfp_id": rfp_id,
            "source": "matrix",
            "total": total,
            "section_count": len(sections),
            "sections": sections,
        }


@app.get("/api/v1/rfp/{rfp_id}/requirements/document")
async def get_document_requirements(
    rfp_id: str,
    payload: dict = Depends(require_role()),
    skip: int = 0,
    offset: Optional[int] = None,
    limit: int = Query(50, ge=1, le=500),
):
    """
    LLM-extracted document requirements (Dataset B): inferred from PDF/DOCX text.
    """
    tenant_uuid = uuid.UUID(payload["tenant_id"])
    page_offset = skip if offset is None else offset
    # Only include rows explicitly marked as textual (LLM-extracted).
    # Previously rows with NULL req_type were treated as textual which could
    # surface spreadsheet-derived rows in the document view. Be explicit.
    textual_filter = models_db.Requirement.req_type == "textual"
    # Never surface Excel-sourced rows in document (LLM) view — matrix only
    not_spreadsheet_source = or_(
        models_db.Requirement.source_file.is_(None),
        and_(
            ~func.lower(models_db.Requirement.source_file).like("%.xlsx"),
            ~func.lower(models_db.Requirement.source_file).like("%.xlsm"),
        ),
    )

    async with AsyncSessionLocal() as db:
        await _get_rfp_for_tenant(db, rfp_id, tenant_uuid)
        base = [
            models_db.Requirement.rfp_id == rfp_id,
            textual_filter,
            not_spreadsheet_source,
        ]
        total = (
            await db.execute(
                select(func.count(models_db.Requirement.req_id)).where(*base)
            )
        ).scalar_one()
        rows = (
            await db.execute(
                select(models_db.Requirement)
                .where(*base)
                .order_by(
                    models_db.Requirement.source_file.asc().nullsfirst(),
                    models_db.Requirement.source_page.asc().nullsfirst(),
                    models_db.Requirement.req_code.asc().nullslast(),
                )
                .offset(page_offset)
                .limit(limit)
            )
        ).scalars().all()

        return {
            "rfp_id": rfp_id,
            "source": "document",
            "total": int(total),
            "skip": page_offset,
            "limit": limit,
            "requirements": [_serialize_requirement_row(r) for r in rows],
        }


@app.get("/api/v1/rfp/{rfp_id}/requirements")
async def get_rfp_requirements(
    rfp_id: str,
    payload: dict = Depends(require_role()),
    skip: int = 0,
    offset: Optional[int] = None,
    limit: int = Query(50, ge=1, le=500),
    req_type: Optional[str] = Query(
        None,
        description="Filter rows: matrix (spreadsheet) or textual (document / LLM). Omit for combined list.",
    ),
):
    if req_type is not None and req_type not in ("matrix", "textual"):
        raise HTTPException(status_code=400, detail="req_type must be 'matrix' or 'textual'")

    tenant_uuid = uuid.UUID(payload["tenant_id"])
    page_offset = skip if offset is None else offset

    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == tenant_uuid,
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")

        base_req = [models_db.Requirement.rfp_id == rfp_id]

        total_all = (
            await db.execute(
                select(func.count(models_db.Requirement.req_id)).where(*base_req)
            )
        ).scalar_one()

        cnt_matrix = (
            await db.execute(
                select(func.count(models_db.Requirement.req_id)).where(
                    *base_req,
                    models_db.Requirement.req_type == "matrix",
                )
            )
        ).scalar_one()

        cnt_textual = (
            await db.execute(
                select(func.count(models_db.Requirement.req_id)).where(
                    *base_req,
                    models_db.Requirement.req_type == "textual",
                )
            )
        ).scalar_one()

        cat_rows = (
            await db.execute(
                select(models_db.Requirement.category, func.count(models_db.Requirement.req_id))
                .where(*base_req, models_db.Requirement.req_type == "matrix")
                .group_by(models_db.Requirement.category)
            )
        ).all()
        matrix_category_counts = {str(row[0] or "Uncategorized"): int(row[1]) for row in cat_rows}

        filters = list(base_req)
        if req_type == "matrix":
            filters.append(models_db.Requirement.req_type == "matrix")
        elif req_type == "textual":
            filters.append(
                or_(
                    models_db.Requirement.req_type == "textual",
                    models_db.Requirement.req_type.is_(None),
                )
            )

        total_filtered = (
            await db.execute(
                select(func.count(models_db.Requirement.req_id)).where(*filters)
            )
        ).scalar_one()

        requirements = (
            await db.execute(
                select(models_db.Requirement)
                .where(*filters)
                .order_by(
                    models_db.Requirement.req_type.asc().nullsfirst(),
                    models_db.Requirement.source_sheet.asc().nullsfirst(),
                    models_db.Requirement.source_file.asc().nullsfirst(),
                    models_db.Requirement.req_code.asc().nullslast(),
                )
                .offset(page_offset)
                .limit(limit)
            )
        ).scalars().all()

        return {
            "rfp_id": rfp_id,
            "total": total_filtered,
            "total_all": total_all,
            "counts_by_type": {
                "matrix": int(cnt_matrix),
                "textual": int(cnt_textual),
            },
            "matrix_category_counts": matrix_category_counts,
            "skip": page_offset,
            "limit": limit,
            "requirements": [_serialize_requirement_row(r) for r in requirements],
        }


# ── Vendor response ───────────────────────────────────────────────────────────
def _enforce_rubric_locked(rfp_row, rfp_id: str, vendor_name: str, acknowledge_unlocked: bool) -> bool:
    """
    Issue #6 gate: do not grade vendors against an unapproved rubric.

    - Rubric locked        → returns False (normal path, nothing to audit).
    - Unlocked + enforced  → raises HTTP 409 unless the caller passed an
      explicit acknowledge_unlocked_rubric override.
    - Unlocked + overridden/disabled → logs a warning and returns True so the
      caller records an audit event for the deliberate override.
    """
    if rfp_row.rubric_locked_at is not None:
        return False
    if settings.REQUIRE_LOCKED_RUBRIC and not acknowledge_unlocked:
        raise HTTPException(
            409,
            "Rubric is not locked for this RFP. Grading now would compute scores "
            f"against unapproved weights. Lock it first "
            f"(POST /api/v1/rfp/{rfp_id}/rubric/lock), or resubmit with "
            "acknowledge_unlocked_rubric=true to deliberately grade against the "
            "draft rubric.",
        )
    logger.warning(
        "Grading RFP %s vendor '%s' against an UNLOCKED rubric "
        "(explicit override=%s). Scores use unreviewed criteria weights.",
        rfp_id, vendor_name, acknowledge_unlocked,
    )
    return True


async def _enqueue_vendor_grade(
    rfp_id: str, response_id: str, vendor_name: str, zip_bytes: bytes, task_id: str
) -> None:
    """
    Stage the uploaded ZIP in S3 and enqueue the durable Celery grading task
    (issue #4). Grading no longer runs inside the API process, so it survives
    restarts and does not block the web server. The raw bytes go through S3
    rather than the Redis broker to keep large uploads out of the message queue.
    """
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name="us-east-1",
    )
    bucket = settings.S3_BUCKET
    try:
        await asyncio.to_thread(s3.head_bucket, Bucket=bucket)
    except Exception:
        await asyncio.to_thread(s3.create_bucket, Bucket=bucket)

    key = f"staging/vendor/{response_id}.zip"
    await asyncio.to_thread(
        s3.put_object, Bucket=bucket, Key=key, Body=zip_bytes,
        ContentType="application/zip",
    )

    from app.worker import grade_vendor
    grade_vendor.apply_async(
        args=[rfp_id, response_id, vendor_name, key, task_id], task_id=task_id,
    )
    logger.info(
        "Enqueued grade_vendor task=%s response=%s (staged %s)",
        task_id, response_id, key,
    )


@app.post("/api/v1/rfp/{rfp_id}/vendor", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def add_vendor(
    rfp_id: str,
    background_tasks: BackgroundTasks,
    file:         UploadFile    = File(...),
    vendor_name:  str           = Form(...),
    submitted_at: Optional[str] = Form(None),
    acknowledge_unlocked_rubric: bool = Form(False),
    payload:      dict          = Depends(require_role("admin")),
):
    try:
        parsed_submitted_at = _parse_datetime(submitted_at)
    except ValueError:
        raise HTTPException(400, "Invalid submitted_at")

    async with AsyncSessionLocal() as db:
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise HTTPException(404, "RFP not found")

        # Issue #6 gate: refuse to grade against an unapproved rubric (409),
        # unless an admin explicitly overrides with acknowledge_unlocked_rubric.
        rubric_overridden = _enforce_rubric_locked(
            rfp_row, rfp_id, vendor_name, acknowledge_unlocked_rubric
        )

        tenant_id   = str(rfp_row.tenant_id)
        vendor_id   = str(uuid.uuid4())
        response_id = str(uuid.uuid4())
        vendor_row  = models_db.Vendor(
            vendor_id=vendor_id, tenant_id=tenant_id, name=vendor_name, created_at=datetime.utcnow(),
        )
        response_row = models_db.VendorResponse(
            response_id=response_id,
            rfp_id=rfp_id,
            vendor_id=vendor_id,
            tenant_id=tenant_id,
            submitted_at=parsed_submitted_at,
            status="ingesting",
            created_at=datetime.utcnow(),
        )
        db.add(vendor_row)
        db.add(response_row)
        await record_audit_event(
            db,
            action="add_vendor",
            entity_type="vendor_response",
            entity_id=response_id,
            rfp_id=rfp_id,
            tenant_id=tenant_id,
            actor=payload.get("email", "admin"),
            actor_user_id=payload["sub"],
            details={
                "vendor_name": vendor_name,
                "submitted_at": _serialize_datetime(parsed_submitted_at),
                "unlocked_rubric_override": rubric_overridden,
            },
        )
        await db.commit()

    task_id = await new_task(
        f"Process vendor: {vendor_name}",
        rfp_id=rfp_id,
        response_id=response_id,
        tenant_id=tenant_id,
    )
    content = await file.read()
    await _enqueue_vendor_grade(rfp_id, response_id, vendor_name, content, task_id)
    return {
        "response_id": response_id, "vendor_id": vendor_id, "task_id": task_id,
        "stages": ["parsing", "ocr", "embedding→milvus", "coverage", "scoring"],
    }


@app.get("/api/v1/rfp/{rfp_id}/vendor/{response_id}/score")
async def get_score(rfp_id: str, response_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        resp = (
            await db.execute(
                select(models_db.VendorResponse).where(
                    models_db.VendorResponse.response_id == response_id,
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not resp:
            raise HTTPException(404, "Response not found")

        if resp.status == "error":
            task_row = (
                await db.execute(
                    select(models_db.Task)
                    .where(models_db.Task.response_id == response_id)
                    .order_by(models_db.Task.created_at.desc())
                )
            ).scalars().first()
            return {
                "status": "error",
                "error": (task_row.error if task_row and task_row.error else "Vendor evaluation failed"),
                "response_id": response_id,
                "rfp_id": rfp_id,
            }

        vs_row = (
            await db.execute(
                select(models_db.VendorScore)
                .where(models_db.VendorScore.response_id == response_id)
                .order_by(models_db.VendorScore.computed_at.desc())
            )
        ).scalars().first()

        if not vs_row:
            return {"status": resp.status or "processing"}

        req_scores = (
            await db.execute(
                select(models_db.RequirementScore, models_db.Requirement, models_db.CoverageEntry)
                .outerjoin(models_db.Requirement, models_db.Requirement.req_id == models_db.RequirementScore.req_id)
                .outerjoin(
                    models_db.CoverageEntry,
                    (models_db.CoverageEntry.req_id == models_db.RequirementScore.req_id) &
                    (models_db.CoverageEntry.response_id == response_id),
                )
                .where(models_db.RequirementScore.response_id == response_id)
            )
        ).all()

        coverage_total = (
            await db.execute(
                select(func.count(models_db.CoverageEntry.entry_id))
                .where(models_db.CoverageEntry.response_id == response_id)
            )
        ).scalar_one()
        coverage_covered = 0
        if coverage_total:
            coverage_covered = (
                await db.execute(
                    select(func.count(models_db.CoverageEntry.entry_id)).where(
                        models_db.CoverageEntry.response_id == response_id,
                        models_db.CoverageEntry.is_covered.is_(True),
                    )
                )
            ).scalar_one()
        coverage_rate = round(coverage_covered / coverage_total, 4) if coverage_total else 0.0

        requirement_scores = []
        for score, req, coverage in req_scores:
            # ISSUE #9 FIX: expose final_score (COALESCE human/AI) not just raw_score
            final_score = score.human_score if score.human_score is not None else score.raw_score
            # No evidence was retrieved for this requirement (coverage gap). The
            # 0 is provisional, not a judged non-compliance — the UI shows this
            # distinctly as "No evidence" rather than a real score.
            no_evidence = not (coverage and coverage.evidence_text)
            req_obj = {
                "score_id":        str(score.score_id),
                "req_id":          str(score.req_id),
                "req_code":        req.req_code if req else None,
                "description":     req.description if req else None,
                "category":        req.category if req else None,
                "raw_score":       score.raw_score,
                "ai_score":        score.ai_score,
                "human_score":     score.human_score,
                "final_score":     final_score,
                "confidence_flag": score.confidence_flag,
                "no_evidence":     no_evidence,
                "compliance_level": score.compliance_level,
                "justification":   score.justification,
                "reasoning":       score.reasoning,
                "override_reason": score.override_reason,
                "scored_at":       score.scored_at.isoformat() if score.scored_at else None,
                "criterion_id":    str(score.criterion_id) if score.criterion_id else None,
                "evidence_quote":  score.evidence_quote,
                "evidence_source": score.evidence_source,
                "evidence_page":   score.evidence_page,
                "evidence_link":   score.evidence_link,
                "matrix_sheet_order": req.matrix_sheet_order if req else None,
                "matrix_row_order":   req.matrix_row_order if req else None,
                "source_sheet":    req.source_sheet if req else None,
            }
            if coverage:
                req_obj["evidence"] = {
                    "is_covered":       coverage.is_covered,
                    "is_gap":           coverage.is_gap,
                    "similarity_score": coverage.similarity_score,
                    "evidence_page":    coverage.evidence_page,
                    "evidence_text":    coverage.evidence_text,
                    "deep_link":        coverage.deep_link,
                }
            requirement_scores.append(req_obj)
        requirement_scores = [r for r in requirement_scores if r["req_id"] not in (None, "None")]

        # ── Score trace: declared vs verified per requirement, criterion scores
        # computed by the same aggregation as the total.
        criteria_rows = (
            await db.execute(
                select(models_db.EvaluationCriterion).where(models_db.EvaluationCriterion.rfp_id == rfp_id)
            )
        ).scalars().all()
        declaration_rows = (
            await db.execute(
                select(models_db.VendorDeclaration).where(models_db.VendorDeclaration.response_id == response_id)
            )
        ).scalars().all()
        compliance_levels = (
            await db.execute(
                select(models_db.RFPPackage.compliance_levels).where(models_db.RFPPackage.rfp_id == rfp_id)
            )
        ).scalar_one_or_none() or list(DEFAULT_COMPLIANCE_LEVELS)

        trace = None
        try:
            trace = build_trace(
                criteria=[
                    {
                        "criterion_id":    str(c.criterion_id),
                        "parent_id":       str(c.parent_id) if c.parent_id else None,
                        "label":           c.label or "",
                        "section":         c.section or c.label or "",
                        "weight":          float(c.weight or 0.0),
                        "criterion_type":  c.criterion_type or "scored",
                        "evaluation_mode": c.evaluation_mode,
                    }
                    for c in criteria_rows
                ],
                scores=requirement_scores + [
                    # criterion-level direct scores (no requirement) still count
                    {"req_id": None, "criterion_id": str(sc.criterion_id), "raw_score": sc.raw_score,
                     "human_score": sc.human_score, "compliance_level": sc.compliance_level, "category": ""}
                    for sc, rq, _cov in req_scores if not sc.req_id and sc.criterion_id
                ],
                declarations={
                    d.req_code: {
                        "declared_level": d.declared_level,
                        "declared_raw":   d.declared_raw,
                        "response_text":  d.response_text,
                        "reference":      d.reference,
                    }
                    for d in declaration_rows if d.req_code
                },
                compliance_levels=compliance_levels,
            )
        except Exception as exc:   # the trace must never break the score page
            logger.warning("Score trace failed for response %s: %s", response_id, exc, exc_info=True)

        return {
            "vendor_score": {
                "total_score":   vs_row.total_score,
                "section_scores": vs_row.section_scores or {},
                "computed_at":   vs_row.computed_at.isoformat() if vs_row.computed_at else None,
            },
            "requirement_scores": requirement_scores,
            # Share of requirements for which the proposal contains evidence
            # (verified level other than Not Addressed). The former figure —
            # retrieval similarity above a threshold — was close to 100% for
            # every vendor because some passage is always retrieved.
            "coverage_rate": (trace or {}).get("summary", {}).get("evidence_rate", coverage_rate) if trace else coverage_rate,
            "retrieval_coverage_rate": coverage_rate,
            "trace": trace,
        }


@app.post("/api/v1/rfp/{rfp_id}/vendor/{response_id}/retry", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def retry_vendor_evaluation(rfp_id: str, response_id: str, background_tasks: BackgroundTasks, file: UploadFile = File(...), acknowledge_unlocked_rubric: bool = Form(False), payload: dict = Depends(require_role("admin"))):
    async with AsyncSessionLocal() as db:
        response_row = (
            await db.execute(
                select(models_db.VendorResponse).where(
                    models_db.VendorResponse.response_id == response_id,
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not response_row:
            raise HTTPException(404, "Response not found")

        vendor_row = (
            await db.execute(select(models_db.Vendor).where(models_db.Vendor.vendor_id == response_row.vendor_id))
        ).scalar_one_or_none()
        if not vendor_row:
            raise HTTPException(404, "Vendor not found")

        if response_row.status not in {"error", "ingesting", "scoring"}:
            raise HTTPException(400, f"Response is in '{response_row.status}' state and cannot be retried")

        # Issue #6 gate: a retry re-runs scoring, so enforce the same rubric
        # lock requirement as the initial submission.
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise HTTPException(404, "RFP not found")
        _enforce_rubric_locked(rfp_row, rfp_id, vendor_row.name, acknowledge_unlocked_rubric)

        response_row.status = "ingesting"
        await db.commit()
        vendor_name = vendor_row.name
        tenant_id   = str(response_row.tenant_id)

    content = await file.read()
    task_id = await new_task(
        f"Retry vendor: {vendor_name}",
        rfp_id=rfp_id,
        response_id=response_id,
        tenant_id=tenant_id,
    )
    await _enqueue_vendor_grade(rfp_id, response_id, vendor_name, content, task_id)
    return {
        "response_id": response_id, "task_id": task_id, "status": "retrying",
        "stages": ["parsing", "ocr", "embedding→milvus", "coverage", "scoring"],
    }


@app.post("/api/v1/rfp/{rfp_id}/vendor/{response_id}/score", dependencies=[Depends(RateLimiter(times=100, seconds=60))])
async def update_score(rfp_id: str, response_id: str, body: dict, payload: dict = Depends(require_role("admin"))):
    criterion_name = (body.get("criterion") or "").strip()
    try:
        new_score = float(body.get("score"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid score value")
    if not criterion_name:
        raise HTTPException(400, "Missing criterion")

    async with AsyncSessionLocal() as db:
        response_row = (
            await db.execute(
                select(models_db.VendorResponse).where(
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.tenant_id == payload["tenant_id"],
                    models_db.VendorResponse.response_id == response_id,
                )
            )
        ).scalar_one_or_none()

        if response_row is None:
            response_row = (
                await db.execute(
                    select(models_db.VendorResponse)
                    .join(models_db.Vendor)
                    .where(
                        models_db.VendorResponse.rfp_id == rfp_id,
                        models_db.VendorResponse.tenant_id == payload["tenant_id"],
                        models_db.Vendor.name == response_id,
                    )
                )
            ).scalar_one_or_none()

        if not response_row:
            raise HTTPException(404, "Response not found")

        criterion_row = (
            await db.execute(
                select(models_db.EvaluationCriterion).where(
                    models_db.EvaluationCriterion.rfp_id == rfp_id,
                    (
                        (models_db.EvaluationCriterion.label == criterion_name)
                        | (models_db.EvaluationCriterion.section == criterion_name)
                    ),
                )
            )
        ).scalar_one_or_none()
        if not criterion_row:
            raise HTTPException(404, "Evaluation criterion not found")

        max_allowed = float(criterion_row.max_score or 5)
        if new_score < 0 or new_score > max_allowed:
            raise HTTPException(400, f"Score {new_score} out of range [0, {max_allowed}]")

        score_row = (
            await db.execute(
                select(models_db.VendorScore).where(
                    models_db.VendorScore.response_id == response_row.response_id,
                    models_db.VendorScore.rfp_id == rfp_id,
                )
            )
        ).scalars().first()

        criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion).where(models_db.EvaluationCriterion.rfp_id == rfp_id)
            )
        ).scalars().all()

        previous_score    = None
        created_new_score = False

        if score_row is None:
            initial_scores = {criterion_row.label: new_score}
            total_score    = _calculate_weighted_total(initial_scores, criteria)
            score_row      = models_db.VendorScore(
                rfp_id=rfp_id,
                response_id=response_row.response_id,
                total_score=total_score,
                section_scores=initial_scores,
                computed_at=datetime.utcnow(),
            )
            db.add(score_row)
            created_new_score = True
        else:
            section_scores = dict(score_row.section_scores or {})
            previous_score = section_scores.get(criterion_row.label)
            section_scores[criterion_row.label] = new_score
            score_row.section_scores = section_scores
            score_row.total_score    = _calculate_weighted_total(section_scores, criteria)
            score_row.computed_at    = datetime.utcnow()
            total_score              = score_row.total_score

        await record_audit_event(
            db,
            action="update_requirement_score",
            tenant_id=payload["tenant_id"],
            actor=payload.get("email", "admin"),
            actor_user_id=payload["sub"],
            entity_type="vendor_score",
            entity_id=f"{response_row.response_id}:{criterion_row.criterion_id}",
            rfp_id=rfp_id,
            details={
                "response_id": str(response_row.response_id),
                "criterion": criterion_row.label,
                "previous_score": previous_score,
                "created_new_score": created_new_score,
                "new_score": new_score,
                "total_score": total_score,
            },
        )
        await db.commit()
        return {
            "status": "updated",
            "rfp_id": rfp_id,
            "response_id": str(response_row.response_id),
            "criterion": criterion_name,
            "score": new_score,
            "total_score": total_score,
        }


@app.get("/api/v1/rfp/{rfp_id}/vendor/{response_id}/coverage")
async def get_coverage(rfp_id: str, response_id: str, gaps_only: bool = False, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        resp = (
            await db.execute(
                select(models_db.VendorResponse).where(
                    models_db.VendorResponse.response_id == response_id,
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not resp:
            raise HTTPException(404, "Response not found")

        coverage_rows = (
            await db.execute(
                select(models_db.CoverageEntry, models_db.Requirement)
                .join(models_db.Requirement, models_db.Requirement.req_id == models_db.CoverageEntry.req_id)
                .where(models_db.CoverageEntry.response_id == response_id)
            )
        ).all()

        if not coverage_rows:
            raise HTTPException(404, "Evaluation not complete")

        entries       = []
        covered_count = 0
        gap_count     = 0
        partial_count = 0

        for cov, req in coverage_rows:
            if gaps_only and not cov.is_gap:
                continue
            entries.append({
                "req_id":               str(cov.req_id),
                "req_code":             req.req_code,
                "description":          req.description,
                "category":             req.category,
                "is_mandatory":         req.is_mandatory,
                "is_covered":           cov.is_covered,
                "is_gap":               cov.is_gap,
                "similarity_score":     round(cov.similarity_score, 3),
                "evidence_text":        cov.evidence_text,
                "evidence_page":        cov.evidence_page,
                "evidence_bounding_box":cov.evidence_bounding_box,
                "deep_link":            cov.deep_link,
                "best_claim_id":        str(cov.best_claim_id) if cov.best_claim_id else None,
            })
            if cov.is_covered:
                covered_count += 1
            elif cov.is_gap:
                gap_count += 1
            else:
                partial_count += 1

        total        = len(entries)
        coverage_rate = round(covered_count / total, 4) if total else 0.0

        return {
            "rfp_id": rfp_id, "response_id": response_id,
            "coverage_rate": coverage_rate, "total_requirements": total,
            "covered_count": covered_count, "partial_count": partial_count,
            "gap_count": gap_count, "entries": entries,
        }


@app.get("/api/v1/rfp/{rfp_id}/vendor/{response_id}/team")
async def get_team(rfp_id: str, response_id: str, skill: Optional[str] = None, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        response_row = (
            await db.execute(
                select(models_db.VendorResponse).where(
                    models_db.VendorResponse.response_id == response_id,
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not response_row:
            raise HTTPException(404, "Response not found")

        members = (
            await db.execute(select(models_db.TeamMember).where(models_db.TeamMember.response_id == response_id))
        ).scalars().all()

        result = []
        for member in members:
            skills = member.skills or []
            if skill and not any(skill.lower() in str(item).lower() for item in skills):
                continue
            result.append({
                "member_id":          str(member.member_id),
                "full_name":          member.full_name,
                "role":               member.role,
                "years_experience":   member.years_experience,
                "skills":             skills,
                "certifications":     member.certifications or [],
                "education":          member.education or [],
                "source_page":        member.source_page,
                "source_bounding_box":member.source_bounding_box,
            })
        return {"members": result}


@app.get("/api/v1/rfp/{rfp_id}/compare")
async def compare_vendors(rfp_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])

        criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion)
                .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
                .order_by(models_db.EvaluationCriterion.label.asc())
            )
        ).scalars().all()

        responses = (
            await db.execute(
                select(models_db.VendorResponse.response_id, models_db.Vendor.name, models_db.VendorResponse.status,
                       models_db.VendorResponse.created_at)
                .join(models_db.Vendor, models_db.Vendor.vendor_id == models_db.VendorResponse.vendor_id)
                .where(models_db.VendorResponse.rfp_id == rfp_id)
                .order_by(models_db.VendorResponse.created_at.asc())
            )
        ).all()

        latest_scores = {}
        score_rows = (
            await db.execute(
                select(models_db.VendorScore)
                .where(models_db.VendorScore.rfp_id == rfp_id)
                .order_by(models_db.VendorScore.computed_at.desc())
            )
        ).scalars().all()
        for row in score_rows:
            latest_scores.setdefault(str(row.response_id), row)

        vendors, vendor_details, totals, section_data = [], [], {}, {}
        for response_id, vendor_name, status, created_at in responses:
            vendors.append(vendor_name)
            vendor_details.append({"name": vendor_name, "response_id": str(response_id), "status": status,
                                   "uploaded_at": created_at.isoformat() if created_at else None})
            score_row = latest_scores.get(str(response_id))
            if not score_row:
                totals[vendor_name] = 0.0
                continue
            totals[vendor_name] = score_row.total_score or 0.0
            for label, score in (score_row.section_scores or {}).items():
                section_data.setdefault(label, {})[vendor_name] = score

        # Criteria in grid order with their effective weights. Leaves carry the
        # vendors' criterion scores; a parent's score is the weighted average
        # of its children (the SUMPRODUCT of the evaluation matrix), so parent
        # rows no longer show 0.0 / NaN.
        crit_dicts = [
            {
                "criterion_id":   str(c.criterion_id),
                "parent_id":      str(c.parent_id) if c.parent_id else None,
                "label":          c.label or "",
                "section":        c.section or c.label or "",
                "weight":         float(c.weight or 0.0),
                "criterion_type": c.criterion_type or "scored",
            }
            for c in criteria
        ]
        effective = compute_effective_weights(crit_dicts)
        leaves, _parents = _select_leaf_criteria(crit_dicts)
        leaf_ids = {c["criterion_id"] for c in leaves}
        children: dict[str, list[dict]] = {}
        for c in crit_dicts:
            if c["parent_id"]:
                children.setdefault(c["parent_id"], []).append(c)

        def _scores_of(c: dict) -> dict:
            if c["criterion_id"] in leaf_ids:
                return section_data.get(c["label"], {})
            kids = children.get(c["criterion_id"], [])
            out = {}
            for v in vendors:
                pairs = [(_scores_of(k).get(v), effective.get(k["criterion_id"], 0.0)) for k in kids]
                pairs = [(sc, w) for sc, w in pairs if sc is not None and w > 0]
                wsum = sum(w for _, w in pairs)
                if wsum:
                    out[v] = round(sum(sc * w for sc, w in pairs) / wsum, 3)
            return out

        def _code_key(c: dict):
            code = _criterion_code(c) or ""
            return [int(x) for x in re.findall(r"\d+", code)] or [10 ** 6], c["label"]

        ordered: list[dict] = []

        def _walk(parent_id, depth):
            for c in sorted([x for x in crit_dicts if x["parent_id"] == parent_id], key=_code_key):
                ordered.append((c, depth))
                _walk(c["criterion_id"], depth + 1)

        _walk(None, 0)
        criteria_out = [
            {
                "criterion_id":     c["criterion_id"],
                "parent_id":        c["parent_id"],
                "code":             _criterion_code(c),
                "label":            c["label"],
                "weight":           c["weight"],
                "effective_weight": round(effective.get(c["criterion_id"], 0.0), 6),
                "is_leaf":          c["criterion_id"] in leaf_ids,
                "depth":            depth,
                "scores":           _scores_of(c),
            }
            for c, depth in ordered
        ]
        ranking = sorted(vendors, key=lambda v: totals.get(v, 0), reverse=True)
        return {
            "vendors": vendors, "vendor_details": vendor_details,
            "criteria": criteria_out, "totals": totals, "ranking": ranking,
        }


@app.get("/api/v1/rfp/{rfp_id}/config")
async def get_config(rfp_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])
        criteria = (await db.execute(select(models_db.EvaluationCriterion).where(models_db.EvaluationCriterion.rfp_id == rfp_id))).scalars().all()
        return {
            "rfp_id": rfp_id,
            "scoring_margin": rfp.scoring_margin if rfp.scoring_margin is not None else 10,
            "compliance_levels": rfp.compliance_levels if rfp.compliance_levels else list(DEFAULT_COMPLIANCE_LEVELS),
            "criteria": [
                {"label": c.label, "weight": c.weight, "section": c.section, "max_score": c.max_score}
                for c in criteria
            ],
        }


@app.post("/api/v1/rfp/{rfp_id}/config")
async def update_config(rfp_id: str, body: dict, payload: dict = Depends(require_role("admin"))):
    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])

        previous = {
            "scoring_margin":   rfp.scoring_margin if rfp.scoring_margin is not None else 10,
            "compliance_levels": rfp.compliance_levels if rfp.compliance_levels else list(DEFAULT_COMPLIANCE_LEVELS),
        }

        if "scoring_margin" in body:
            rfp.scoring_margin = _parse_margin(body.get("scoring_margin"), previous["scoring_margin"])
        if "compliance_levels" in body:
            rfp.compliance_levels = _parse_compliance_levels(body.get("compliance_levels"))

        updated = {
            "scoring_margin":   rfp.scoring_margin if rfp.scoring_margin is not None else 10,
            "compliance_levels": rfp.compliance_levels if rfp.compliance_levels else list(DEFAULT_COMPLIANCE_LEVELS),
        }

        await record_audit_event(db, action="update_config", entity_type="rfp_package",
                                 entity_id=rfp_id, rfp_id=rfp_id, tenant_id=payload["tenant_id"],
                                 actor=payload.get("email", "admin"), actor_user_id=payload["sub"],
                                 details={"before": previous, "after": updated})
        await db.commit()
        return {"status": "updated", "rfp_id": rfp_id, **updated}


@app.post("/api/v1/rfp/{rfp_id}/report")
async def generate_report(rfp_id: str, body: ReportRequest, payload: dict = Depends(require_role())):
    report_id = str(uuid.uuid4())
    task_id   = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        rfp_row = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])

        _, generate_report_pdf = get_report_worker()

        tid = str(rfp_row.tenant_id)
        db.add(models_db.Task(
            task_id=task_id, tenant_id=tid, description=f"Generate report: {report_id}",
            status="queued", progress=0, created_at=datetime.utcnow(),
            started_at=datetime.utcnow(), rfp_id=rfp_id,
        ))
        db.add(models_db.Report(
            report_id=report_id, rfp_id=rfp_id, tenant_id=tid, task_id=task_id,
            report_type=body.report_type, format=body.format,
            vendor_ids=body.vendor_ids or [], status="pending",
            created_at=datetime.utcnow(),
        ))
        await db.commit()

    task = generate_report_pdf.apply_async(args=[report_id, rfp_id], task_id=task_id)
    logger.info(f"Queued PDF report generation: report_id={report_id}, task_id={task.id}")

    return {
        "report_id": report_id, "task_id": str(task.id), "rfp_id": rfp_id,
        "status": "queued",
        "download_url": f"/api/v1/reports/{report_id}/download",
        "status_url":   f"/api/v1/reports/{report_id}/status",
        "message": "Report generation queued. Use status_url to track progress.",
    }


@app.post("/api/v1/rfp/{rfp_id}/report/audit-csv")
async def generate_audit_csv_report(rfp_id: str, payload: dict = Depends(require_role("admin", "ai_auditor"))):
    """Queue an audit trail CSV export for an RFP."""
    from app.worker import generate_audit_csv
    report_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        rfp_row = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])
        tid = str(rfp_row.tenant_id)
        db.add(models_db.Report(report_id=report_id, rfp_id=rfp_id, tenant_id=tid, report_type="audit_csv", format="csv", status="pending", created_at=datetime.utcnow()))
        db.add(models_db.Task(task_id=report_id, tenant_id=tid, description=f"Audit CSV: {rfp_id}", status="queued", progress=0, created_at=datetime.utcnow(), started_at=datetime.utcnow(), rfp_id=rfp_id))
        await db.commit()
    generate_audit_csv.apply_async(args=[report_id, rfp_id], task_id=report_id)
    return {"report_id": report_id, "status": "queued", "download_url": f"/api/v1/reports/{report_id}/download"}


@app.post("/api/v1/rfp/{rfp_id}/report/compliance-certificate")
async def generate_compliance_cert_report(rfp_id: str, payload: dict = Depends(require_role("admin"))):
    """Queue a compliance certificate PDF for a locked RFP."""
    from app.worker import generate_compliance_certificate
    report_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        rfp_row = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])
        if rfp_row.status != "locked":
            raise HTTPException(400, "Compliance certificate is only available for locked RFPs")
        tid = str(rfp_row.tenant_id)
        db.add(models_db.Report(report_id=report_id, rfp_id=rfp_id, tenant_id=tid, report_type="compliance_certificate", format="pdf", status="pending", created_at=datetime.utcnow()))
        db.add(models_db.Task(task_id=report_id, tenant_id=tid, description=f"Compliance cert: {rfp_id}", status="queued", progress=0, created_at=datetime.utcnow(), started_at=datetime.utcnow(), rfp_id=rfp_id))
        await db.commit()
    generate_compliance_certificate.apply_async(args=[report_id, rfp_id], task_id=report_id)
    return {"report_id": report_id, "status": "queued", "download_url": f"/api/v1/reports/{report_id}/download"}


@app.post("/api/v1/rfp/{rfp_id}/report/vendor-debriefs")
async def generate_all_vendor_debriefs(rfp_id: str, payload: dict = Depends(require_role("admin"))):
    """Queue debrief PDF for every vendor on an RFP. Returns one report_id per vendor."""
    from app.worker import generate_vendor_debrief
    async with AsyncSessionLocal() as db:
        rfp_row = await _get_rfp_for_tenant(db, rfp_id, payload["tenant_id"])
        responses = (await db.execute(select(models_db.VendorResponse, models_db.Vendor).join(models_db.Vendor, models_db.Vendor.vendor_id == models_db.VendorResponse.vendor_id).where(models_db.VendorResponse.rfp_id == rfp_id))).all()
        queued = []
        tid = str(rfp_row.tenant_id)
        for resp, vendor in responses:
            report_id = str(uuid.uuid4())
            db.add(models_db.Report(report_id=report_id, rfp_id=rfp_id, tenant_id=tid, report_type="vendor_debrief", format="pdf", vendor_ids=[str(vendor.vendor_id)], status="pending", created_at=datetime.utcnow()))
            db.add(models_db.Task(task_id=report_id, tenant_id=tid, description=f"Debrief: {vendor.name}", status="queued", progress=0, created_at=datetime.utcnow(), started_at=datetime.utcnow(), rfp_id=rfp_id, response_id=resp.response_id))
            generate_vendor_debrief.apply_async(args=[report_id, rfp_id, str(resp.response_id)], task_id=report_id)
            queued.append({"report_id": report_id, "vendor_name": vendor.name, "download_url": f"/api/v1/reports/{report_id}/download"})
        await db.commit()
    return {"queued": len(queued), "reports": queued}


@app.get("/api/v1/reports/{report_id}/status")
async def get_report_status(report_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        report_row = (
            await db.execute(select(models_db.Report).where(
                models_db.Report.report_id == report_id,
                models_db.Report.tenant_id == payload["tenant_id"],
            ))
        ).scalar_one_or_none()
        if not report_row:
            raise HTTPException(404, f"Report {report_id} not found")

        response = {
            "report_id":   str(report_row.report_id),
            "rfp_id":      str(report_row.rfp_id),
            "status":      report_row.status,
            "report_type": report_row.report_type,
            "format":      report_row.format,
            "created_at":  report_row.created_at.isoformat() if report_row.created_at else None,
            "completed_at":report_row.completed_at.isoformat() if report_row.completed_at else None,
        }

        if report_row.task_id:
            response["celery_task_id"] = str(report_row.task_id)
            try:
                celery_app, _ = get_report_worker()
                task_result   = celery_app.AsyncResult(str(report_row.task_id))
                response["celery_status"]   = task_result.status
                response["celery_progress"] = task_result.info if task_result.status == "PROGRESS" else None
            except HTTPException as exc:
                response["celery_status"] = "unavailable"
                response["celery_error"]  = exc.detail

        if report_row.status == "failed":
            response["error_message"] = report_row.error_message

        return response


@app.get("/api/v1/docs/{doc_id}/content")
async def get_document_content(doc_id: str, payload: dict = Depends(require_role())):
    """Return the raw document as a Base64-encoded payload for in-browser viewing."""
    import base64
    tenant_uuid = uuid.UUID(payload["tenant_id"])
    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(
                select(models_db.Document).where(models_db.Document.doc_id == doc_id)
            )
        ).scalar_one_or_none()

        if not doc:
            raise HTTPException(404, f"Document {doc_id} not found")

        # Tenant isolation via the owning RFP (or response)
        if doc.rfp_id:
            rfp_check = (
                await db.execute(
                    select(models_db.RFPPackage).where(
                        models_db.RFPPackage.rfp_id == str(doc.rfp_id),
                        models_db.RFPPackage.tenant_id == str(tenant_uuid),
                    )
                )
            ).scalar_one_or_none()
            if not rfp_check:
                raise HTTPException(403, "Access denied")
        elif doc.tenant_id and str(doc.tenant_id) != str(tenant_uuid):
            raise HTTPException(403, "Access denied")

        if not doc.file_path or not doc.file_path.startswith("s3://"):
            raise HTTPException(500, "Document has no valid storage path")

        ext = (doc.file_name or "").rsplit(".", 1)[-1].lower() if "." in (doc.file_name or "") else ""
        content_type_map = {
            "pdf":  "application/pdf",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xls":  "application/vnd.ms-excel",
            "csv":  "text/csv",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        content_type = content_type_map.get(ext, "application/octet-stream")

    try:
        s3_client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name="us-east-1",
        )
        path_parts = doc.file_path[5:].split("/", 1)
        bucket = path_parts[0]
        key    = path_parts[1] if len(path_parts) > 1 else ""
        s3_resp = s3_client.get_object(Bucket=bucket, Key=key)
        raw = s3_resp["Body"].read()
        return {
            "doc_id":         doc_id,
            "filename":       doc.file_name,
            "content_type":   content_type,
            "file_size":      len(raw),
            "content_base64": base64.b64encode(raw).decode("utf-8"),
        }
    except Exception as e:
        logger.error("get_document_content error for %s: %s", doc_id, e, exc_info=True)
        raise HTTPException(500, f"Failed to retrieve document: {e}")


@app.get("/api/v1/reports/{report_id}/download")
async def download_report(report_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        report_row = (
            await db.execute(select(models_db.Report).where(
                models_db.Report.report_id == report_id,
                models_db.Report.tenant_id == payload["tenant_id"],
            ))
        ).scalar_one_or_none()
        if not report_row:
            raise HTTPException(404, f"Report {report_id} not found")

        if report_row.status in ("pending", "processing"):
            return {
                "status": 202,
                "message": "Report is still being generated",
                "status_url": f"/api/v1/reports/{report_id}/status",
            }

        if report_row.status == "failed":
            raise HTTPException(400, f"Report generation failed: {report_row.error_message}")

        if not report_row.file_path:
            raise HTTPException(500, "Report completed but file path is missing")

        try:
            s3_client = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT,
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                region_name="us-east-1",
            )

            if report_row.file_path.startswith("s3://"):
                path_parts = report_row.file_path[5:].split("/", 1)
                bucket = path_parts[0]
                key    = path_parts[1] if len(path_parts) > 1 else ""
            else:
                raise ValueError(f"Invalid S3 path: {report_row.file_path}")

            response = s3_client.get_object(Bucket=bucket, Key=key)

            # Derive content-type and filename from the stored format/type fields
            fmt = (report_row.format or "pdf").lower()
            rtype = (report_row.report_type or "report").lower()
            if fmt == "csv" or rtype in ("audit_csv", "audit-csv"):
                media_type = "text/csv"
                ext = "csv"
            elif fmt == "zip":
                media_type = "application/zip"
                ext = "zip"
            else:
                media_type = "application/pdf"
                ext = "pdf"

            safe_type = rtype.replace("_", "-")
            filename = f"rfp-{safe_type}-{report_id[:8]}.{ext}"

            return StreamingResponse(
                io.BytesIO(response["Body"].read()),
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as e:
            logger.error(f"Error downloading report {report_id}: {e}", exc_info=True)
            raise HTTPException(500, f"Failed to download report: {e}")


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, payload: dict = Depends(require_role())):
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(models_db.Task).where(
                models_db.Task.task_id == task_id,
                models_db.Task.tenant_id == payload["tenant_id"],
            ))).scalar_one_or_none()
        if not row:
            raise HTTPException(404, "Task not found")
        return {
            "task_id":     str(row.task_id),
            "description": row.description,
            "status":      row.status,
            "progress":    row.progress,
            "result":      row.result,
            "error":       row.error,
            "created_at":  row.created_at.isoformat() if row.created_at else None,
            "started_at":  row.started_at.isoformat() if row.started_at else None,
            "completed_at":row.completed_at.isoformat() if row.completed_at else None,
            "rfp_id":      str(row.rfp_id) if row.rfp_id else None,
            "response_id": str(row.response_id) if row.response_id else None,
        }


@app.get("/api/v1/audit-log")
async def list_audit_log(
    rfp_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    payload: dict = Depends(require_role("admin", "ai_auditor")),
):
    async with AsyncSessionLocal() as db:
        query = select(models_db.AuditLog).where(models_db.AuditLog.tenant_id == payload["tenant_id"])
        if rfp_id:
            query = query.where(models_db.AuditLog.rfp_id == rfp_id)
        if action:
            query = query.where(models_db.AuditLog.action == action)

        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows  = (
            await db.execute(
                query.order_by(models_db.AuditLog.created_at.desc())
                .offset(max(offset, 0))
                .limit(min(max(limit, 1), 200))
            )
        ).scalars().all()

        return {
            "total": total,
            "items": [
                {
                    "audit_id":    str(row.audit_id),
                    "rfp_id":      str(row.rfp_id) if row.rfp_id else None,
                    "entity_type": row.entity_type,
                    "entity_id":   row.entity_id,
                    "action":      row.action,
                    "actor":       row.actor,
                    "details":     row.details or {},
                    "created_at":  row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        }


# ── Chat sessions ─────────────────────────────────────────────────────────────
class ChatArtifactCreate(BaseModel):
    artifact_type: str
    content: str
    citations: list[dict] = Field(default_factory=list)


@app.post("/api/v1/rfp/{rfp_id}/chat/session", dependencies=[Depends(RateLimiter(times=20, seconds=60))])
async def create_session(rfp_id: str, payload: dict = Depends(get_current_user)):
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise HTTPException(404, "RFP not found")

        if rfp_row.status in ("ingesting", "error"):
            raise HTTPException(
                400,
                f"RFP is not ready for chat (status: {rfp_row.status}). "
                "Wait until ingestion completes.",
            )

        sid = str(uuid.uuid4())
        db.add(models_db.ChatSession(
            session_id=sid,
            rfp_id=rfp_id,
            tenant_id=tenant_id,
            started_at=datetime.utcnow(),
            last_active=datetime.utcnow(),
        ))
        await db.commit()
    return {"session_id": sid, "rfp_id": rfp_id}


@app.get("/api/v1/rfp/{rfp_id}/chat/session/{session_id}/messages")
async def get_messages(
    rfp_id: str,
    session_id: str,
    payload: dict = Depends(get_current_user),
):
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        session_row = (
            await db.execute(
                select(models_db.ChatSession).where(
                    models_db.ChatSession.session_id == session_id,
                    models_db.ChatSession.rfp_id == rfp_id,
                    models_db.ChatSession.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not session_row:
            raise HTTPException(404, "Session not found")

        messages = (
            await db.execute(
                select(models_db.ChatMessage)
                .where(models_db.ChatMessage.session_id == session_id)
                .order_by(models_db.ChatMessage.created_at.asc())
            )
        ).scalars().all()

        return {
            "messages": [
                {
                    "role":       message.role,
                    "content":    message.content,
                    "citations":  message.citations or [],
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                }
                for message in messages
            ]
        }


@app.get("/api/v1/rfp/{rfp_id}/chat/artifacts")
async def get_chat_artifacts(
    rfp_id: str,
    payload: dict = Depends(get_current_user),
):
    tenant_id = payload["tenant_id"]
    user_id = payload["sub"]
    async with AsyncSessionLocal() as db:
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise HTTPException(404, "RFP not found")

        rows = (
            await db.execute(
                select(models_db.ChatArtifact)
                .where(
                    models_db.ChatArtifact.rfp_id == rfp_id,
                    models_db.ChatArtifact.tenant_id == tenant_id,
                    models_db.ChatArtifact.user_id == user_id,
                )
                .order_by(models_db.ChatArtifact.saved_at.desc())
            )
        ).scalars().all()

    pinned = []
    summaries = []
    compares = []
    for row in rows:
        item = {
            "artifact_id": str(row.artifact_id),
            "artifact_type": row.artifact_type,
            "content": row.content,
            "citations": row.citations or [],
            "saved_at": row.saved_at.isoformat() if row.saved_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        if row.artifact_type == "pin":
            pinned.append(item)
        elif row.artifact_type == "summary":
            summaries.append(item)
        elif row.artifact_type == "compare":
            compares.append(item)

    return {"pinned": pinned, "summaries": summaries, "compares": compares}


@app.post("/api/v1/rfp/{rfp_id}/chat/artifacts", status_code=201)
async def create_chat_artifact(
    rfp_id: str,
    body: ChatArtifactCreate,
    payload: dict = Depends(get_current_user),
):
    tenant_id = payload["tenant_id"]
    user_id = payload["sub"]
    artifact_type = body.artifact_type.strip().lower()
    if artifact_type not in {"pin", "summary", "compare"}:
        raise HTTPException(400, "artifact_type must be 'pin', 'summary', or 'compare'")
    if not body.content.strip():
        raise HTTPException(400, "content is required")

    async with AsyncSessionLocal() as db:
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise HTTPException(404, "RFP not found")

        artifact = models_db.ChatArtifact(
            tenant_id=tenant_id,
            rfp_id=rfp_id,
            user_id=user_id,
            artifact_type=artifact_type,
            content=body.content.strip(),
            citations=body.citations or [],
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)

    return {
        "artifact_id": str(artifact.artifact_id),
        "artifact_type": artifact.artifact_type,
        "content": artifact.content,
        "citations": artifact.citations or [],
        "saved_at": artifact.saved_at.isoformat() if artifact.saved_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }


@app.delete("/api/v1/rfp/{rfp_id}/chat/artifacts/{artifact_id}")
async def delete_chat_artifact(
    rfp_id: str,
    artifact_id: str,
    payload: dict = Depends(get_current_user),
):
    tenant_id = payload["tenant_id"]
    user_id = payload["sub"]
    async with AsyncSessionLocal() as db:
        artifact_row = (
            await db.execute(
                select(models_db.ChatArtifact).where(
                    models_db.ChatArtifact.artifact_id == artifact_id,
                    models_db.ChatArtifact.rfp_id == rfp_id,
                    models_db.ChatArtifact.tenant_id == tenant_id,
                    models_db.ChatArtifact.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if not artifact_row:
            raise HTTPException(404, "Artifact not found")

        await db.delete(artifact_row)
        await db.commit()

    return {"ok": True}


# ── WebSocket: streaming RAG chatbot ─────────────────────────────────────────
@app.websocket("/ws/chat/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str):
    try:
        claims = decode_token(ws.query_params.get("token") or "")
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    async with AsyncSessionLocal() as db:
        session_row = (
            await db.execute(select(models_db.ChatSession)
                .join(models_db.RFPPackage, models_db.RFPPackage.rfp_id == models_db.ChatSession.rfp_id)
                .where(
                    models_db.ChatSession.session_id == session_id,
                    models_db.RFPPackage.tenant_id == claims.get("tenant_id"),
                )
            )
        ).scalar_one_or_none()
        if not session_row:
            await ws.send_json({"type": "error", "message": "Session not found"})
            await ws.close()
            return

        history_rows = (
            await db.execute(
                select(models_db.ChatMessage)
                .where(models_db.ChatMessage.session_id == session_id)
                .order_by(models_db.ChatMessage.created_at.asc())
            )
        ).scalars().all()

    session = {
        "session_id": session_id,
        "rfp_id":     str(session_row.rfp_id),
        "history":    [{"role": row.role, "content": row.content} for row in history_rows],
    }
    rfp_id = session["rfp_id"]
    vs     = get_vector_store()

    # Determine workflow stage: Stage A = no vendor responses, Stage B = vendors present.
    async with AsyncSessionLocal() as db:
        _vendor_count = (
            await db.execute(
                select(func.count(models_db.VendorResponse.response_id))
                .where(models_db.VendorResponse.rfp_id == rfp_id)
            )
        ).scalar_one()
    has_vendors = _vendor_count > 0

    async def search_database(query: str, vendor_name: str | None = None) -> list[dict]:
        collections = [
            col_vendor_chunks(rfp_id),
            col_rfp_chunks(rfp_id),
            col_requirements(rfp_id),
            col_cv_skills(rfp_id),
        ]
        from app.services.llm import transform_query
        
        # Transform original query into 3 semantic variations
        variations = await transform_query(query)
        # Search original query + variations, deduplicated
        all_queries = list(set([query] + variations))
        
        async def _single_search(q: str) -> list[dict]:
            # Run the synchronous search_multi in a separate worker thread
            return await asyncio.to_thread(
                vs.search_multi,
                collection_names=collections,
                query=q,
                top_k=8,
                rfp_id=rfp_id,
            )

        results_lists = await asyncio.gather(*[_single_search(q) for q in all_queries])
        
        # Merge and deduplicate hits by hit ID, keeping the highest score
        merged_hits: dict[str, dict] = {}
        for hits in results_lists:
            for hit in hits:
                hit_id = hit.get("id")
                if not hit_id:
                    continue
                if hit_id not in merged_hits or hit.get("score", 0.0) > merged_hits[hit_id].get("score", 0.0):
                    merged_hits[hit_id] = hit
                    
        # Sort merged hits by score descending
        sorted_hits = sorted(merged_hits.values(), key=lambda x: x.get("score", 0.0), reverse=True)
        
        # Filter by vendor name if specified
        if vendor_name:
            vendor_key = vendor_name.lower()
            sorted_hits = [
                hit for hit in sorted_hits
                if vendor_key in str(hit.get("metadata", {}).get("vendor_name", "")).lower()
                or vendor_key in str(hit.get("metadata", {}).get("file_name", "")).lower()
            ]
            
        return sorted_hits[:8]

    async def get_vendor_pricing(v_name: str) -> dict:
        async with AsyncSessionLocal() as db:
            vendor_res = (
                await db.execute(
                    select(models_db.VendorResponse, models_db.Vendor.name)
                    .join(models_db.Vendor, models_db.Vendor.vendor_id == models_db.VendorResponse.vendor_id)
                    .where(models_db.VendorResponse.rfp_id == rfp_id)
                    .where(models_db.Vendor.name.ilike(f"%{v_name}%"))
                )
            ).first()
            if not vendor_res:
                return {"error": f"Vendor matching '{v_name}' not found under this RFP."}
            
            response_id = vendor_res.VendorResponse.response_id
            matched_name = vendor_res.name
            
            claims = (
                await db.execute(
                    select(models_db.VendorClaim)
                    .where(models_db.VendorClaim.response_id == response_id)
                    .where(models_db.VendorClaim.claim_type == "Pricing")
                    .limit(20)
                )
            ).scalars().all()
            
            docs = (
                await db.execute(
                    select(models_db.Document)
                    .where(models_db.Document.response_id == response_id)
                    .where(models_db.Document.doc_role == "Pricing")
                )
            ).scalars().all()
            
            doc_chunks = []
            if docs:
                doc_ids = [d.doc_id for d in docs]
                doc_chunks = (
                    await db.execute(
                        select(models_db.DocChunk)
                        .where(models_db.DocChunk.doc_id.in_(doc_ids))
                        .limit(20)
                    )
                ).scalars().all()
            
            return {
                "vendor_name": matched_name,
                "pricing_claims": [
                    {
                        "claim_text": c.claim_text,
                        "source_page": c.source_page,
                        "source_section": c.source_section
                    } for c in claims
                ],
                "pricing_documents": [
                    {
                        "file_name": d.file_name,
                        "page_count": d.page_count
                    } for d in docs
                ],
                "pricing_excerpts": [
                    {
                        "file_name": next((d.file_name for d in docs if d.doc_id == chunk.doc_id), "Document"),
                        "page_number": chunk.page_number,
                        "section_heading": chunk.section_heading,
                        "content": chunk.content[:1000]
                    } for chunk in doc_chunks
                ]
            }

    async def compare_vendors_cb(req_id: str, v_a: str, v_b: str) -> dict:
        async with AsyncSessionLocal() as db:
            vendor_a_res = (
                await db.execute(
                    select(models_db.VendorResponse, models_db.Vendor.name)
                    .join(models_db.Vendor, models_db.Vendor.vendor_id == models_db.VendorResponse.vendor_id)
                    .where(models_db.VendorResponse.rfp_id == rfp_id)
                    .where(models_db.Vendor.name.ilike(f"%{v_a}%"))
                )
            ).first()
            vendor_b_res = (
                await db.execute(
                    select(models_db.VendorResponse, models_db.Vendor.name)
                    .join(models_db.Vendor, models_db.Vendor.vendor_id == models_db.VendorResponse.vendor_id)
                    .where(models_db.VendorResponse.rfp_id == rfp_id)
                    .where(models_db.Vendor.name.ilike(f"%{v_b}%"))
                )
            ).first()

            if not vendor_a_res:
                return {"error": f"Vendor A matching '{v_a}' not found under this RFP."}
            if not vendor_b_res:
                return {"error": f"Vendor B matching '{v_b}' not found under this RFP."}

            req_query = select(models_db.Requirement).where(models_db.Requirement.rfp_id == rfp_id)
            req_query = req_query.where(
                (models_db.Requirement.req_code.ilike(req_id.strip())) |
                (models_db.Requirement.description.ilike(f"%{req_id}%"))
            )
            reqs = (await db.execute(req_query)).scalars().all()
            if not reqs:
                return {
                    "error": f"No requirement matching '{req_id}' was found in this RFP.",
                    "vendor_a": vendor_a_res.name,
                    "vendor_b": vendor_b_res.name
                }

            comparison_results = []
            for req in reqs:
                scores_a = (
                    await db.execute(
                        select(models_db.RequirementScore)
                        .where(models_db.RequirementScore.req_id == req.req_id)
                        .where(models_db.RequirementScore.response_id == vendor_a_res.VendorResponse.response_id)
                    )
                ).scalars().all()
                scores_b = (
                    await db.execute(
                        select(models_db.RequirementScore)
                        .where(models_db.RequirementScore.req_id == req.req_id)
                        .where(models_db.RequirementScore.response_id == vendor_b_res.VendorResponse.response_id)
                    )
                ).scalars().all()

                cov_a = (
                    await db.execute(
                        select(models_db.CoverageEntry)
                        .where(models_db.CoverageEntry.req_id == req.req_id)
                        .where(models_db.CoverageEntry.response_id == vendor_a_res.VendorResponse.response_id)
                    )
                ).scalars().first()
                cov_b = (
                    await db.execute(
                        select(models_db.CoverageEntry)
                        .where(models_db.CoverageEntry.req_id == req.req_id)
                        .where(models_db.CoverageEntry.response_id == vendor_b_res.VendorResponse.response_id)
                    )
                ).scalars().first()

                comparison_results.append({
                    "req_code": req.req_code,
                    "description": req.description,
                    "category": req.category,
                    "is_mandatory": req.is_mandatory,
                    vendor_a_res.name: {
                        "scores": [
                            {
                                "evaluator_role": s.evaluator_role,
                                "raw_score": s.raw_score,
                                "compliance_level": s.compliance_level,
                                "justification": s.justification,
                                "reasoning": s.reasoning
                            } for s in scores_a
                        ],
                        "evidence": cov_a.evidence_text if cov_a else None,
                        "evidence_page": cov_a.evidence_page if cov_a else None
                    },
                    vendor_b_res.name: {
                        "scores": [
                            {
                                "evaluator_role": s.evaluator_role,
                                "raw_score": s.raw_score,
                                "compliance_level": s.compliance_level,
                                "justification": s.justification,
                                "reasoning": s.reasoning
                            } for s in scores_b
                        ],
                        "evidence": cov_b.evidence_text if cov_b else None,
                        "evidence_page": cov_b.evidence_page if cov_b else None
                    }
                })

            return {
                "vendor_a": vendor_a_res.name,
                "vendor_b": vendor_b_res.name,
                "comparisons": comparison_results
            }

    async def get_rfp_matrix_weights() -> dict:
        async with AsyncSessionLocal() as db:
            criteria = (
                await db.execute(
                    select(models_db.EvaluationCriterion)
                    .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
                )
            ).scalars().all()
            return {
                "rfp_id": rfp_id,
                "criteria": [
                    {
                        "criterion_id": str(c.criterion_id),
                        "label": c.label,
                        "section": c.section,
                        "weight": c.weight,
                        "max_score": c.max_score,
                        "criterion_type": c.criterion_type,
                        "owner_role": c.owner_role
                    } for c in criteria
                ]
            }

    async def get_rfp_overview() -> dict:
        async with AsyncSessionLocal() as db:
            total_reqs = (
                await db.execute(
                    select(func.count(models_db.Requirement.req_id))
                    .where(models_db.Requirement.rfp_id == rfp_id)
                )
            ).scalar_one()

            mandatory_count = (
                await db.execute(
                    select(func.count(models_db.Requirement.req_id))
                    .where(models_db.Requirement.rfp_id == rfp_id)
                    .where(models_db.Requirement.is_mandatory == True)
                )
            ).scalar_one()

            category_rows = (
                await db.execute(
                    select(
                        models_db.Requirement.category,
                        func.count(models_db.Requirement.req_id).label("cnt"),
                    )
                    .where(models_db.Requirement.rfp_id == rfp_id)
                    .group_by(models_db.Requirement.category)
                    .order_by(func.count(models_db.Requirement.req_id).desc())
                )
            ).all()

            criteria = (
                await db.execute(
                    select(models_db.EvaluationCriterion)
                    .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
                )
            ).scalars().all()

            rfp_row = (
                await db.execute(
                    select(models_db.RFPPackage)
                    .where(models_db.RFPPackage.rfp_id == rfp_id)
                )
            ).scalar_one_or_none()

            return {
                "rfp_id": rfp_id,
                "rfp_title": rfp_row.title if rfp_row else "Unknown",
                "status": rfp_row.status if rfp_row else "Unknown",
                "total_requirements": total_reqs,
                "mandatory_requirements": mandatory_count,
                "optional_requirements": total_reqs - mandatory_count,
                "categories": {row.category: row.cnt for row in category_rows},
                "evaluation_criteria": [
                    {
                        "label": c.label,
                        "section": c.section,
                        "weight": c.weight,
                        "max_score": c.max_score,
                        "criterion_type": c.criterion_type,
                    }
                    for c in criteria
                ],
                "note": (
                    f"This RFP has {total_reqs} extracted requirements "
                    f"({mandatory_count} mandatory, {total_reqs - mandatory_count} optional). "
                    "No vendor responses have been submitted yet."
                ),
            }

    async def get_vendor_rankings() -> dict:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(models_db.VendorScore, models_db.Vendor.name)
                    .join(models_db.VendorResponse,
                          models_db.VendorResponse.response_id == models_db.VendorScore.response_id)
                    .join(models_db.Vendor,
                          models_db.Vendor.vendor_id == models_db.VendorResponse.vendor_id)
                    .where(models_db.VendorResponse.rfp_id == rfp_id)
                    .order_by(models_db.VendorScore.total_score.desc())
                )
            ).all()

            vendors_out = []
            for rank, (score, vendor_name) in enumerate(rows, 1):
                # Compliance breakdown
                breakdown_rows = (
                    await db.execute(
                        select(
                            models_db.RequirementScore.compliance_level,
                            func.count(models_db.RequirementScore.score_id).label("count"),
                        )
                        .where(models_db.RequirementScore.response_id == score.response_id)
                        .group_by(models_db.RequirementScore.compliance_level)
                    )
                ).all()
                breakdown = {r.compliance_level: r.count for r in breakdown_rows}
                total_reqs = sum(breakdown.values()) or 1

                # Flagged (low-confidence) requirement ratio
                flagged_count = (
                    await db.execute(
                        select(func.count(models_db.RequirementScore.score_id))
                        .where(models_db.RequirementScore.response_id == score.response_id)
                        .where(models_db.RequirementScore.confidence_flag == True)
                    )
                ).scalar_one()
                flagged_ratio = round(flagged_count / total_reqs, 2)

                # Label each section score as strength / neutral / gap
                section_scores = score.section_scores or {}
                section_interpretation = {}
                for criterion, sc in section_scores.items():
                    if sc >= 3.5:
                        label = "strength"
                    elif sc >= 2.5:
                        label = "neutral"
                    elif sc > 0:
                        label = "gap"
                    else:
                        label = "not_scored"
                    section_interpretation[criterion] = {
                        "score": round(sc, 2),
                        "interpretation": label,
                    }

                vendors_out.append({
                    "rank":           rank,
                    "vendor_name":    vendor_name,
                    "total_score":    round(score.total_score, 2),
                    "max_score":      5.0,
                    "section_scores": section_interpretation,
                    "compliance_breakdown": breakdown,
                    "flagged_ratio":  flagged_ratio,
                    "scoring_note": (
                        "HIGH uncertainty — over 30% of scores were flagged for human review"
                        if flagged_ratio > 0.30
                        else "Scoring reliability is acceptable"
                    ),
                })

            # Decision confidence based on score spread between rank 1 and rank 2
            if len(vendors_out) == 0:
                decision_confidence = "N/A — no vendor scores available"
            elif len(vendors_out) == 1:
                decision_confidence = "N/A — only one vendor evaluated; ranking requires at least two vendors"
            else:
                decision_confidence = ""  # will be set below
            if len(vendors_out) >= 2:
                spread = vendors_out[0]["total_score"] - vendors_out[1]["total_score"]
                if spread < 0.1:
                    decision_confidence = (
                        f"LOW — score gap between rank 1 and rank 2 is only {spread:.2f}/5.0. "
                        "Scores are statistically too close to make a decisive recommendation. "
                        "Human review is strongly advised."
                    )
                elif spread < 0.3:
                    decision_confidence = (
                        f"MODERATE — score gap is {spread:.2f}/5.0. "
                        "Rank 1 is ahead but the margin is narrow."
                    )
                else:
                    decision_confidence = (
                        f"HIGH — score gap is {spread:.2f}/5.0. "
                        "Rank 1 is clearly ahead."
                    )

            return {
                "rfp_id":             rfp_id,
                "vendors":            vendors_out,
                "decision_confidence": decision_confidence,
                "score_thresholds": {
                    "strength":   "section_score >= 3.5",
                    "neutral":    "2.5 <= section_score < 3.5",
                    "gap":        "0 < section_score < 2.5",
                    "not_scored": "section_score = 0 (no evidence found or criterion unscored)",
                },
                "note": (
                    "total_score is weighted across all criteria (max 5.0). "
                    "Use section_scores[criterion].interpretation to classify strengths and gaps — "
                    "do NOT label a score below 3.5 as a strength."
                ),
            }

    try:
        while True:
            data     = await ws.receive_json()
            question = data.get("content", "").strip()
            if data.get("type") != "question" or not question:
                continue

            full_answer = ""
            citations   = []

            async for event in agent_chat_loop(
                question=question,
                history=session["history"],
                search_database=search_database,
                get_vendor_pricing=get_vendor_pricing if has_vendors else None,
                compare_vendors=compare_vendors_cb if has_vendors else None,
                get_rfp_matrix_weights=get_rfp_matrix_weights,
                get_vendor_rankings=get_vendor_rankings if has_vendors else None,
                get_rfp_overview=get_rfp_overview if not has_vendors else None,
            ):
                event_type = event.get("type")
                if event_type == "token":
                    full_answer += event.get("content", "")
                    await ws.send_json({"type": "token", "content": event.get("content", "")})
                elif event_type == "citations":
                    citations = event.get("data", [])
                    await ws.send_json({"type": "citations", "data": citations})
                elif event_type == "thought":
                    await ws.send_json({"type": "thought", "content": event.get("content", "")})
                elif event_type == "done":
                    await ws.send_json({"type": "done"})

            session["history"].extend([
                {"role": "user",      "content": question},
                {"role": "assistant", "content": full_answer},
            ])

            async with AsyncSessionLocal() as db:
                session_row = (
                    await db.execute(select(models_db.ChatSession).where(models_db.ChatSession.session_id == session_id))
                ).scalar_one_or_none()
                if session_row:
                    session_row.last_active = datetime.utcnow()
                    db.add(models_db.ChatMessage(session_id=session_id, role="user", content=question, created_at=datetime.utcnow()))
                    db.add(models_db.ChatMessage(session_id=session_id, role="assistant", content=full_answer, citations=citations, created_at=datetime.utcnow()))
                    await db.commit()

    except WebSocketDisconnect:
        logger.info(f"WS disconnected: {session_id}")
    except Exception as e:
        logger.error(f"WS error: {e}", exc_info=True)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass



