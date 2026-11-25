"""
Celery worker for background PDF report generation.

FIXES IN THIS VERSION
─────────────────────
Issue 13 — generate_report_pdf created a NEW event loop with asyncio.new_event_loop()
           on every call and never reused it across the try/except error-handling
           block. The error handler also created ANOTHER new loop, meaning two
           event loops could exist simultaneously in one thread. On Python 3.11+
           this raises a DeprecationWarning; on 3.12+ it raises RuntimeError.
           Fix: use asyncio.run() instead, which correctly creates, runs, and
           closes a loop in one call. One loop per task, no leaks.

Issue 14 — _fetch_report_data fetched vendor names with N separate SELECT
           queries inside a loop (one per vendor response). For 20 vendors
           that is 20 round-trips.  Fix: single IN query fetches all vendors
           at once, then dict lookup replaces the per-vendor DB call.

Issue 15 — _generate_pdf_html built the rankings list with a manual enumerate
           then re-ranked with a second enumerate, producing off-by-one rank
           numbers (rank=2 for the top vendor when only one existed, etc.).
           Fix: clean single-pass sort → re-rank.

Issue 16 — generate_report_pdf called s3_client.create_bucket unconditionally
           and caught only BucketAlreadyOwnedByYou.  On AWS (not MinIO) the
           correct exception is ClientError with code BucketAlreadyExists, and
           on MinIO in some configs it is also a ClientError.  This caused
           silent failures where reports were generated but never uploaded.
           Fix: use head_bucket to check existence first, catch ClientError.
"""
import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from io import BytesIO

from celery import Celery
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import boto3
from botocore.exceptions import ClientError

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models import db as models_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

celery_app = Celery(
    "rfp_evaluator",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)
celery_app.autodiscover_tasks(["app"])

# Durability (issue #4): ack a task only AFTER it finishes, and re-queue it if
# the worker dies mid-task, so an API/worker restart no longer loses an in-flight
# grade. prefetch=1 keeps a slow grade from blocking other queued tasks.
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)

# Per-task DB session factory.  Each Celery task creates a fresh async engine
# inside asyncio.run() and stores its session maker here so that all helper
# functions automatically use an engine bound to the CURRENT event loop.
# Without this, the module-level engine (created at import time on a different
# loop) causes "Future attached to a different loop" on every task invocation.
_task_db_factory: ContextVar = ContextVar("_task_db_factory", default=None)


def _get_db():
    factory = _task_db_factory.get(None)
    return factory if factory is not None else AsyncSessionLocal


def _make_task_engine():
    """Create a fresh async engine + session factory bound to the running loop."""
    eng = create_async_engine(
        settings.DATABASE_URL,
        pool_size=2,
        max_overflow=1,
        pool_pre_ping=True,
    )
    return eng, async_sessionmaker(eng, expire_on_commit=False)


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name="us-east-1",
    )


def _download_bytes(key: str) -> bytes:
    """Fetch a staged object (e.g. the uploaded vendor ZIP) from S3/MinIO."""
    s3 = _get_s3_client()
    return s3.get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()


@celery_app.task(bind=True, name="app.worker.grade_vendor")
def grade_vendor(self, rfp_id: str, response_id: str, vendor_name: str,
                 s3_key: str, task_id: str):
    """
    Durable vendor grading on the Celery worker (issue #4).

    The ingest + scoring pipeline used to run as a FastAPI BackgroundTask inside
    the web process: slow, unscalable, and lost on any API restart. It now runs
    here, off the request path, surviving restarts via acks_late.

    pipeline_vendor lives in app.main and, with its helpers, resolves
    AsyncSessionLocal as a module global at call time. We bind a fresh async
    engine to THIS task's event loop and redirect those globals through it — the
    worker process never serves the API, so patching them here is safe and keeps
    asyncpg connections on the loop that created them.
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    async def _main():
        import app.db.session as session_mod
        import app.main as main_mod

        eng = create_async_engine(
            settings.DATABASE_URL,
            pool_size=10, max_overflow=10, pool_pre_ping=True, pool_timeout=60,
        )
        factory = async_sessionmaker(eng, expire_on_commit=False, autoflush=False)
        session_mod.engine = eng
        session_mod.AsyncSessionLocal = factory   # covers lazy importers
        main_mod.AsyncSessionLocal = factory       # covers app.main grading helpers

        from app.main import pipeline_vendor
        from app.services.vector_store import get_vector_store

        # The API connects Milvus in its startup handler; the worker has no such
        # hook, so establish the connection here before grading touches it.
        vs = get_vector_store()
        for attempt in range(1, 6):
            try:
                await vs.connect()
                break
            except Exception as exc:
                if attempt == 5:
                    raise
                logger.warning("Worker Milvus connect %s/5 failed: %s", attempt, exc)
                await asyncio.sleep(min(2 ** attempt, 10))

        try:
            file_bytes = await asyncio.to_thread(_download_bytes, s3_key)
            await pipeline_vendor(rfp_id, response_id, vendor_name, file_bytes, task_id)
        finally:
            await eng.dispose()

    return asyncio.run(_main())


def _ensure_bucket(s3_client, bucket: str) -> None:
    """
    ISSUE #16 FIX: check bucket existence with head_bucket before creating.
    head_bucket raises ClientError(404) when absent, not when present.
    This works correctly on both AWS S3 and MinIO.
    """
    try:
        s3_client.head_bucket(Bucket=bucket)
        return  # already exists
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket"):
            raise  # unexpected error — re-raise
    # Bucket does not exist — create it
    try:
        s3_client.create_bucket(Bucket=bucket)
        logger.info("Created S3 bucket: %s", bucket)
    except ClientError as exc:
        # Race condition: another worker created it between our check and create
        if exc.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


async def _fetch_report_data(rfp_id: str, vendor_ids: list = None) -> dict:
    """
    Fetch all data for PDF generation in as few DB round-trips as possible.

    ISSUE #14 FIX: replaced the N-query vendor-name loop with a single
    IN query, then a dict lookup. O(N) DB calls → O(1).
    """
    async with _get_db()() as db:
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(models_db.RFPPackage.rfp_id == rfp_id)
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise ValueError(f"RFP {rfp_id} not found")

        criteria_rows = (
            await db.execute(
                select(models_db.EvaluationCriterion)
                .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
            )
        ).scalars().all()

        requirements_rows = (
            await db.execute(
                select(models_db.Requirement).where(models_db.Requirement.rfp_id == rfp_id)
            )
        ).scalars().all()

        response_query = select(models_db.VendorResponse).where(
            models_db.VendorResponse.rfp_id == rfp_id
        )
        if vendor_ids:
            response_query = response_query.where(
                models_db.VendorResponse.vendor_id.in_(vendor_ids)
            )
        vendor_responses = (await db.execute(response_query)).scalars().all()

        # ISSUE #14 FIX: single query for all vendors
        all_vendor_ids = list({r.vendor_id for r in vendor_responses})
        vendors_map: dict[str, models_db.Vendor] = {}
        if all_vendor_ids:
            vendor_rows = (
                await db.execute(
                    select(models_db.Vendor).where(
                        models_db.Vendor.vendor_id.in_(all_vendor_ids)
                    )
                )
            ).scalars().all()
            vendors_map = {v.vendor_id: v for v in vendor_rows}

        # Single query for all vendor scores
        response_ids = [r.response_id for r in vendor_responses]
        vendor_scores: dict[str, models_db.VendorScore] = {}
        if response_ids:
            score_rows = (
                await db.execute(
                    select(models_db.VendorScore).where(
                        models_db.VendorScore.response_id.in_(response_ids)
                    )
                )
            ).scalars().all()
            vendor_scores = {str(s.response_id): s for s in score_rows}

        # Single query for all requirement scores
        requirement_scores: dict[tuple, list] = {}
        if response_ids:
            req_score_rows = (
                await db.execute(
                    select(models_db.RequirementScore, models_db.Requirement)
                    .outerjoin(
                        models_db.Requirement,
                        models_db.Requirement.req_id == models_db.RequirementScore.req_id,
                    )
                    .where(models_db.RequirementScore.response_id.in_(response_ids))
                )
            ).all()
            for rs, req in req_score_rows:
                key = str(rs.response_id)
                requirement_scores.setdefault(key, []).append({
                    "compliance_level": rs.compliance_level or "NotAddressed",
                    "justification":    rs.justification or "",
                    "description":      req.description if req else "N/A",
                    "category":         req.category    if req else "N/A",
                })

        # Single query for all coverage entries
        vendor_coverage: dict[str, dict] = {}
        if response_ids:
            coverage_rows = (
                await db.execute(
                    select(models_db.CoverageEntry).where(
                        models_db.CoverageEntry.response_id.in_(response_ids)
                    )
                )
            ).scalars().all()
            for ce in coverage_rows:
                resp = next((r for r in vendor_responses if r.response_id == ce.response_id), None)
                if resp:
                    vid = resp.vendor_id
                    bucket = vendor_coverage.setdefault(vid, {"covered": 0, "gaps": 0})
                    if ce.is_covered:
                        bucket["covered"] += 1
                    elif ce.is_gap:
                        bucket["gaps"] += 1

        return {
            "rfp":              rfp_row,
            "criteria":         criteria_rows,
            "requirements":     requirements_rows,
            "vendor_responses": vendor_responses,
            "vendors_map":      vendors_map,
            "vendor_scores":    vendor_scores,
            "requirement_scores": requirement_scores,
            "vendor_coverage":  vendor_coverage,
        }


async def _generate_procurement_narrative(report_data: dict) -> dict:
    """
    Call the LLM to write a formal procurement narrative for the report.
    Returns: {executive_summary, vendor_summaries, risk_flags}
    Gracefully returns a fallback dict if the LLM call fails — the report
    still renders without the narrative rather than the whole task failing.
    """
    from app.core.groq_rotation import get_groq_client
    import re as _re

    groq = get_groq_client()

    vendor_responses = report_data["vendor_responses"]
    vendors_map      = report_data["vendors_map"]
    vendor_scores    = report_data["vendor_scores"]

    unranked = []
    for response in vendor_responses:
        vendor = vendors_map.get(response.vendor_id)
        score  = vendor_scores.get(str(response.response_id))
        if vendor and score:
            unranked.append({
                "vendor_name":    vendor.name,
                "total_score":    score.total_score,
                "section_scores": score.section_scores or {},
            })
    unranked.sort(key=lambda x: x["total_score"], reverse=True)

    scores_text = "\n".join(
        f"  {i+1}. {r['vendor_name']}: {r['total_score']:.2f}/5.0"
        f" | Sections: {', '.join(f'{k}={v:.1f}' for k, v in r['section_scores'].items())}"
        for i, r in enumerate(unranked)
    )

    rfp    = report_data["rfp"]
    prompt = f"""You are a senior procurement officer writing the executive summary for an RFP evaluation report.

RFP: {rfp.title}
Issuing organisation: {rfp.issuing_company or 'Not specified'}

VENDOR SCORES (out of 5.0):
{scores_text}

Write a formal procurement evaluation narrative. Return ONLY a JSON object (no markdown, no backticks):
{{
  "executive_summary": "2-3 sentence overall recommendation identifying the top vendor and why. Formal, decisive tone.",
  "vendor_summaries": {{
    "<vendor_name>": {{
      "strengths": "1-2 specific strengths based on their section scores",
      "concerns": "1-2 specific concerns or gaps, or None identified if scores are strong",
      "recommendation": "Recommend | Consider | Do not recommend"
    }}
  }},
  "risk_flags": [
    "Any cross-cutting risks, gaps, or evaluation concerns worth flagging to decision-makers"
  ]
}}

Be specific. Reference actual score differences. Maximum 3 risk flags."""

    try:
        result = await groq.chat_text(
            [{"role": "user", "content": prompt}],
            model=settings.GROQ_MAIN_MODEL,
            max_tokens=800,
            temperature=0.1,
        )
        cleaned = _re.sub(r"```(?:json)?", "", result).strip().strip("`").strip()
        import json as _json
        return _json.loads(cleaned)
    except Exception as exc:
        logger.warning("Narrative generation failed (non-fatal): %s", exc)
        return {
            "executive_summary": (
                "Automated narrative generation was unavailable for this report. "
                "Please review the score tables and rankings below."
            ),
            "vendor_summaries": {},
            "risk_flags": [],
        }


def _generate_pdf_html(report_data: dict, narrative: dict = None) -> str:
    """
    Render HTML from Jinja2 template.

    ISSUE #15 FIX: cleaned up the double-enumerate ranking logic.
    Old code:
        for idx, response in enumerate(vendor_responses):
            rankings.append((vendor, idx + 1, score))  # rank = insertion order, wrong
        rankings.sort(...)
        rankings = [(vendor, rank, score) for rank, (vendor, _, score) in enumerate(rankings, 1)]
    The first enumerate produced ranks based on insertion order (not score),
    then the re-enumerate produced ranks 1-N correctly but only after the
    sort — the intermediate rank values were meaningless noise.
    Fix: sort first, then assign rank.
    """
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("vendor_comparison_report.html")

    rfp               = report_data["rfp"]
    criteria          = report_data["criteria"]
    requirements      = report_data["requirements"]
    vendor_responses  = report_data["vendor_responses"]
    vendors_map       = report_data["vendors_map"]
    vendor_scores     = report_data["vendor_scores"]
    requirement_scores = report_data["requirement_scores"]
    vendor_coverage   = report_data["vendor_coverage"]

    # ISSUE #15 FIX: build unsorted list, sort by score, then assign rank
    unranked = []
    for response in vendor_responses:
        vendor = vendors_map.get(response.vendor_id)
        score  = vendor_scores.get(str(response.response_id))
        if vendor and score:
            unranked.append({
                "vendor": {
                    "vendor_id":   vendor.vendor_id,
                    "vendor_name": vendor.name,
                    "response_id": str(response.response_id),
                },
                "score": score,
            })

    unranked.sort(key=lambda x: x["score"].total_score, reverse=True)
    rankings = [
        (item["vendor"], rank, item["score"])
        for rank, item in enumerate(unranked, 1)
    ]

    return template.render(
        report_id=str(rfp.rfp_id),
        rfp=rfp,
        criteria=criteria,
        requirements=requirements,
        vendors=[r[0] for r in rankings],
        rankings=rankings,
        vendor_coverage=vendor_coverage,
        requirement_scores=requirement_scores,
        narrative=narrative or {},
        generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


async def _update_report_status(report_id: str, status: str) -> None:
    async with _get_db()() as db:
        row = (
            await db.execute(
                select(models_db.Report).where(models_db.Report.report_id == report_id)
            )
        ).scalar_one_or_none()
        if row:
            row.status = status
            await db.commit()


async def _update_report_success(report_id: str, file_path: str, file_size: int) -> None:
    async with _get_db()() as db:
        row = (
            await db.execute(
                select(models_db.Report).where(models_db.Report.report_id == report_id)
            )
        ).scalar_one_or_none()
        if row:
            row.status       = "completed"
            row.file_path    = file_path
            row.file_size    = file_size
            row.completed_at = datetime.utcnow()
            await db.commit()


async def _update_report_error(report_id: str, error_message: str) -> None:
    async with _get_db()() as db:
        row = (
            await db.execute(
                select(models_db.Report).where(models_db.Report.report_id == report_id)
            )
        ).scalar_one_or_none()
        if row:
            row.status        = "failed"
            row.error_message = error_message
            row.completed_at  = datetime.utcnow()
            await db.commit()


async def _update_task_status(
    task_id: str,
    status: str,
    progress: int | None = None,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    async with _get_db()() as db:
        row = (
            await db.execute(
                select(models_db.Task).where(models_db.Task.task_id == task_id)
            )
        ).scalar_one_or_none()
        if row:
            row.status = status
            if progress is not None:
                row.progress = progress
            if result is not None:
                row.result = result
            if error is not None:
                row.error = error
            if status in ("done", "error"):
                row.completed_at = datetime.utcnow()
            await db.commit()


@celery_app.task(bind=True, name="app.worker.generate_report_pdf")
def generate_report_pdf(self, report_id: str, rfp_id: str):
    """Celery task: generate PDF report and upload to S3/MinIO."""
    task_id = self.request.id
    logger.info("[Task %s] Starting PDF generation for report %s", task_id, report_id)

    async def _main():
        _eng, _session = _make_task_engine()
        _task_db_factory.set(_session)
        try:
            await _update_report_status(report_id, "processing")
            await _update_task_status(task_id, "running", progress=10)

            self.update_state(state="PROGRESS", meta={"current": 10, "total": 100, "status": "Fetching RFP data..."})
            report_data = await _fetch_report_data(rfp_id)

            self.update_state(state="PROGRESS", meta={"current": 25, "total": 100, "status": "Generating procurement narrative..."})
            narrative = await _generate_procurement_narrative(report_data)

            self.update_state(state="PROGRESS", meta={"current": 45, "total": 100, "status": "Rendering report template..."})
            html_content = _generate_pdf_html(report_data, narrative=narrative)

            self.update_state(state="PROGRESS", meta={"current": 60, "total": 100, "status": "Converting to PDF..."})
            pdf_bytes = HTML(string=html_content).write_pdf()

            self.update_state(state="PROGRESS", meta={"current": 80, "total": 100, "status": "Uploading to storage..."})
            s3_client = _get_s3_client()
            file_key  = f"reports/{rfp_id}/{report_id}.pdf"
            _ensure_bucket(s3_client, settings.S3_BUCKET)
            s3_client.put_object(
                Bucket=settings.S3_BUCKET, Key=file_key, Body=pdf_bytes,
                ContentType="application/pdf",
                Metadata={"report_id": report_id, "rfp_id": rfp_id},
            )

            s3_path = f"s3://{settings.S3_BUCKET}/{file_key}"
            self.update_state(state="PROGRESS", meta={"current": 95, "total": 100, "status": "Finalizing..."})
            await _update_report_success(report_id, file_path=s3_path, file_size=len(pdf_bytes))
            await _update_task_status(
                task_id, "done", progress=100,
                result={"report_id": report_id, "file_path": s3_path, "file_size": len(pdf_bytes)},
            )
            logger.info("[Task %s] PDF report %s generated successfully (%d bytes)", task_id, report_id, len(pdf_bytes))
            return {"report_id": report_id, "status": "completed", "file_path": s3_path, "file_size": len(pdf_bytes)}

        except Exception as exc:
            logger.error("[Task %s] Error generating PDF: %s", task_id, exc, exc_info=True)
            await _update_report_error(report_id, str(exc))
            await _update_task_status(task_id, "error", error=str(exc))
            raise
        finally:
            await _eng.dispose()

    return asyncio.run(_main())


@celery_app.task
def dummy_task():
    """Placeholder task to prevent worker startup errors."""
    return "Worker is running"


# ══════════════════════════════════════════════════════════════════════════════
# REPORT TYPE 2 — AUDIT TRAIL CSV
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_audit_data(rfp_id: str) -> dict:
    """Fetch all audit log rows for one RFP."""
    async with _get_db()() as db:
        from sqlalchemy import select as _select
        rfp_row = (
            await db.execute(
                _select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise ValueError(f"RFP {rfp_id} not found")

        rows = (
            await db.execute(
                _select(models_db.AuditLog)
                .where(models_db.AuditLog.rfp_id == rfp_id)
                .order_by(models_db.AuditLog.created_at.asc())
            )
        ).scalars().all()

        return {"rfp": rfp_row, "rows": rows}


def _generate_audit_csv(audit_data: dict) -> bytes:
    """Render the audit log as a UTF-8 CSV."""
    import csv, io, json as _json

    buf = io.StringIO()
    w   = csv.writer(buf)

    # Header
    w.writerow([
        "audit_id", "timestamp", "actor", "actor_user_id",
        "action", "entity_type", "entity_id",
        "old_value", "new_value", "details",
        "ip_address", "user_agent",
    ])

    for row in audit_data["rows"]:
        w.writerow([
            str(row.audit_id),
            row.created_at.isoformat() if row.created_at else "",
            row.actor or "system",
            str(row.actor_user_id) if row.actor_user_id else "",
            row.action or "",
            row.entity_type or "",
            row.entity_id or "",
            _json.dumps(row.old_value) if row.old_value else "",
            _json.dumps(row.new_value) if row.new_value else "",
            _json.dumps(row.details)   if row.details   else "",
            row.ip_address or "",
            row.user_agent or "",
        ])

    return buf.getvalue().encode("utf-8-sig")   # UTF-8 BOM for Excel compat


@celery_app.task(bind=True, name="app.worker.generate_audit_csv")
def generate_audit_csv(self, report_id: str, rfp_id: str):
    """Celery task: export full audit trail as CSV."""
    task_id = self.request.id
    logger.info("[Task %s] Generating audit CSV for RFP %s", task_id, rfp_id)

    async def _main():
        _eng, _session = _make_task_engine()
        _task_db_factory.set(_session)
        try:
            await _update_report_status(report_id, "processing")
            await _update_task_status(task_id, "running", progress=10)

            self.update_state(state="PROGRESS", meta={"current": 20, "total": 100, "status": "Fetching audit log..."})
            audit_data = await _fetch_audit_data(rfp_id)

            self.update_state(state="PROGRESS", meta={"current": 60, "total": 100, "status": "Generating CSV..."})
            csv_bytes = _generate_audit_csv(audit_data)

            self.update_state(state="PROGRESS", meta={"current": 80, "total": 100, "status": "Uploading..."})
            s3_client = _get_s3_client()
            file_key  = f"reports/{rfp_id}/{report_id}_audit.csv"
            _ensure_bucket(s3_client, settings.S3_BUCKET)
            s3_client.put_object(
                Bucket=settings.S3_BUCKET, Key=file_key, Body=csv_bytes,
                ContentType="text/csv",
                Metadata={"report_id": report_id, "rfp_id": rfp_id, "report_type": "audit_csv"},
            )

            s3_path = f"s3://{settings.S3_BUCKET}/{file_key}"
            await _update_report_success(report_id, file_path=s3_path, file_size=len(csv_bytes))
            await _update_task_status(
                task_id, "done", progress=100,
                result={"report_id": report_id, "file_path": s3_path, "rows": len(audit_data["rows"])},
            )
            logger.info("[Task %s] Audit CSV complete — %d rows", task_id, len(audit_data["rows"]))
            return {"report_id": report_id, "status": "completed", "rows": len(audit_data["rows"])}

        except Exception as exc:
            logger.error("[Task %s] Audit CSV failed: %s", task_id, exc, exc_info=True)
            await _update_report_error(report_id, str(exc))
            await _update_task_status(task_id, "error", error=str(exc))
            raise
        finally:
            await _eng.dispose()

    return asyncio.run(_main())


# ══════════════════════════════════════════════════════════════════════════════
# REPORT TYPE 3 — COMPLIANCE CERTIFICATE PDF
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_compliance_data(rfp_id: str) -> dict:
    """Fetch everything needed for the compliance certificate."""
    async with _get_db()() as db:
        from sqlalchemy import select as _select
        rfp_row = (
            await db.execute(
                _select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id
                )
            )
        ).scalar_one_or_none()
        if not rfp_row:
            raise ValueError(f"RFP {rfp_id} not found")

        # Winner
        winner = None
        if rfp_row.winner_vendor_id:
            winner = (
                await db.execute(
                    _select(models_db.Vendor).where(
                        models_db.Vendor.vendor_id == rfp_row.winner_vendor_id
                    )
                )
            ).scalar_one_or_none()

        # Evaluator assignments
        assignments = (
            await db.execute(
                _select(models_db.RFPAssignment, models_db.User)
                .join(models_db.User, models_db.User.user_id == models_db.RFPAssignment.user_id)
                .where(models_db.RFPAssignment.rfp_id == rfp_id)
            )
        ).all()

        # Vendor count
        vendor_count = (
            await db.execute(
                _select(func.count(models_db.VendorResponse.response_id))
                .where(models_db.VendorResponse.rfp_id == rfp_id)
            )
        ).scalar_one()

        # Key audit events
        key_events = (
            await db.execute(
                _select(models_db.AuditLog)
                .where(
                    models_db.AuditLog.rfp_id == rfp_id,
                    models_db.AuditLog.action.in_([
                        "create_rfp", "rubric_locked", "winner_declared",
                        "rfp_locked", "evaluator_submitted",
                    ])
                )
                .order_by(models_db.AuditLog.created_at.asc())
            )
        ).scalars().all()

        return {
            "rfp":         rfp_row,
            "winner":      winner,
            "assignments": assignments,
            "vendor_count": vendor_count,
            "key_events":  key_events,
        }


def _generate_compliance_html(data: dict) -> str:
    template_dir = Path(__file__).parent / "templates"
    env          = Environment(loader=FileSystemLoader(str(template_dir)))
    template     = env.get_template("compliance_certificate.html")
    return template.render(
        rfp=data["rfp"],
        winner=data["winner"],
        assignments=data["assignments"],
        vendor_count=data["vendor_count"],
        key_events=data["key_events"],
        generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        certificate_id=str(__import__("uuid").uuid4())[:8].upper(),
    )


@celery_app.task(bind=True, name="app.worker.generate_compliance_certificate")
def generate_compliance_certificate(self, report_id: str, rfp_id: str):
    """Celery task: generate a formal compliance certificate PDF."""
    task_id = self.request.id
    logger.info("[Task %s] Generating compliance cert for RFP %s", task_id, rfp_id)

    async def _main():
        _eng, _session = _make_task_engine()
        _task_db_factory.set(_session)
        try:
            await _update_report_status(report_id, "processing")
            await _update_task_status(task_id, "running", progress=10)

            self.update_state(state="PROGRESS", meta={"current": 20, "total": 100, "status": "Fetching data..."})
            data = await _fetch_compliance_data(rfp_id)

            self.update_state(state="PROGRESS", meta={"current": 50, "total": 100, "status": "Rendering certificate..."})
            html_content = _generate_compliance_html(data)
            pdf_bytes    = HTML(string=html_content).write_pdf()

            self.update_state(state="PROGRESS", meta={"current": 80, "total": 100, "status": "Uploading..."})
            s3_client = _get_s3_client()
            file_key  = f"reports/{rfp_id}/{report_id}_compliance_cert.pdf"
            _ensure_bucket(s3_client, settings.S3_BUCKET)
            s3_client.put_object(
                Bucket=settings.S3_BUCKET, Key=file_key,
                Body=pdf_bytes, ContentType="application/pdf",
                Metadata={"report_id": report_id, "rfp_id": rfp_id, "report_type": "compliance_certificate"},
            )

            s3_path = f"s3://{settings.S3_BUCKET}/{file_key}"
            await _update_report_success(report_id, file_path=s3_path, file_size=len(pdf_bytes))
            await _update_task_status(
                task_id, "done", progress=100,
                result={"report_id": report_id, "file_path": s3_path},
            )
            logger.info("[Task %s] Compliance certificate complete (%d bytes)", task_id, len(pdf_bytes))
            return {"report_id": report_id, "status": "completed", "file_path": s3_path}

        except Exception as exc:
            logger.error("[Task %s] Compliance cert failed: %s", task_id, exc, exc_info=True)
            await _update_report_error(report_id, str(exc))
            await _update_task_status(task_id, "error", error=str(exc))
            raise
        finally:
            await _eng.dispose()

    return asyncio.run(_main())


# ══════════════════════════════════════════════════════════════════════════════
# REPORT TYPE 4 — VENDOR DEBRIEF PDFs (one per losing vendor)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_debrief_data(rfp_id: str, response_id: str) -> dict:
    """Fetch everything for one vendor's debrief report."""
    async with _get_db()() as db:
        from sqlalchemy import select as _select
        rfp_row = (
            await db.execute(
                _select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id
                )
            )
        ).scalar_one_or_none()

        response = (
            await db.execute(
                _select(models_db.VendorResponse).where(
                    models_db.VendorResponse.response_id == response_id
                )
            )
        ).scalar_one_or_none()

        vendor = (
            await db.execute(
                _select(models_db.Vendor).where(
                    models_db.Vendor.vendor_id == response.vendor_id
                )
            )
        ).scalar_one_or_none()

        criteria = (
            await db.execute(
                _select(models_db.EvaluationCriterion)
                .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
            )
        ).scalars().all()

        scores = (
            await db.execute(
                _select(models_db.RequirementScore, models_db.Requirement)
                .join(models_db.Requirement, models_db.Requirement.req_id == models_db.RequirementScore.req_id)
                .where(models_db.RequirementScore.response_id == response_id)
            )
        ).all()

        vendor_score = (
            await db.execute(
                _select(models_db.VendorScore)
                .where(models_db.VendorScore.response_id == response_id)
                .order_by(models_db.VendorScore.computed_at.desc())
            )
        ).scalars().first()

        # Winner info for comparison (anonymised)
        winner = None
        winner_score = None
        if rfp_row.winner_vendor_id:
            winner = (
                await db.execute(
                    _select(models_db.Vendor).where(
                        models_db.Vendor.vendor_id == rfp_row.winner_vendor_id
                    )
                )
            ).scalar_one_or_none()

            winner_resp = (
                await db.execute(
                    _select(models_db.VendorResponse).where(
                        models_db.VendorResponse.rfp_id    == rfp_id,
                        models_db.VendorResponse.vendor_id == rfp_row.winner_vendor_id,
                    )
                )
            ).scalar_one_or_none()

            if winner_resp:
                winner_score = (
                    await db.execute(
                        _select(models_db.VendorScore)
                        .where(models_db.VendorScore.response_id == winner_resp.response_id)
                        .order_by(models_db.VendorScore.computed_at.desc())
                    )
                ).scalars().first()

        return {
            "rfp":          rfp_row,
            "vendor":       vendor,
            "response":     response,
            "criteria":     criteria,
            "scores":       scores,
            "vendor_score": vendor_score,
            "is_winner":    str(response.vendor_id) == str(rfp_row.winner_vendor_id),
            "winner_total": winner_score.total_score if winner_score else None,
        }


async def _generate_debrief_narrative(data: dict) -> str:
    """LLM-generated debrief narrative for the vendor."""
    from app.core.groq_rotation import get_groq_client
    import re as _re
    groq = get_groq_client()

    vendor_name  = data["vendor"].name if data["vendor"] else "Vendor"
    total        = data["vendor_score"].total_score if data["vendor_score"] else 0
    section_sc   = data["vendor_score"].section_scores if data["vendor_score"] else {}
    is_winner    = data["is_winner"]
    winner_total = data["winner_total"]

    scores_text = ", ".join(f"{k}: {v:.1f}/5" for k, v in section_sc.items()) if section_sc else "N/A"
    outcome     = "selected as the winning vendor" if is_winner else "not selected for this award"

    prompt = f"""You are writing a formal procurement debrief letter for {vendor_name}.

RFP: {data['rfp'].title}
Outcome: {outcome}
{vendor_name} total score: {total:.2f}/5.0
Section scores: {scores_text}
{"Winning vendor total score: " + str(round(winner_total, 2)) + "/5.0" if winner_total and not is_winner else ""}

Write a professional, respectful debrief that:
1. Thanks the vendor for participating
2. States the outcome clearly
3. Explains their relative performance by section (without naming the winning vendor)
4. Highlights 2-3 specific strengths
5. Highlights 1-2 areas for improvement in future bids
6. Closes professionally

Return ONLY plain text — no JSON, no markdown, no headers. 250-350 words."""

    try:
        result = await groq.chat_text(
            [{"role": "user", "content": prompt}],
            model=settings.GROQ_MAIN_MODEL,
            max_tokens=600,
            temperature=0.2,
        )
        return result.strip()
    except Exception as exc:
        logger.warning("Debrief narrative generation failed: %s", exc)
        return (
            f"Thank you for your participation in the {data['rfp'].title} evaluation process. "
            f"After careful review, {vendor_name} achieved a total score of {total:.2f}/5.0. "
            f"We appreciate your time and effort and encourage you to participate in future opportunities."
        )


def _generate_debrief_html(data: dict, narrative: str) -> str:
    template_dir = Path(__file__).parent / "templates"
    env          = Environment(loader=FileSystemLoader(str(template_dir)))
    template     = env.get_template("vendor_debrief.html")
    return template.render(
        rfp=data["rfp"],
        vendor=data["vendor"],
        vendor_score=data["vendor_score"],
        criteria=data["criteria"],
        scores=data["scores"],
        is_winner=data["is_winner"],
        winner_total=data["winner_total"],
        narrative=narrative,
        generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


@celery_app.task(bind=True, name="app.worker.generate_vendor_debrief")
def generate_vendor_debrief(self, report_id: str, rfp_id: str, response_id: str):
    """Celery task: generate a vendor debrief PDF for one vendor."""
    task_id = self.request.id
    logger.info("[Task %s] Generating vendor debrief for response %s", task_id, response_id)

    async def _main():
        _eng, _session = _make_task_engine()
        _task_db_factory.set(_session)
        try:
            await _update_report_status(report_id, "processing")
            await _update_task_status(task_id, "running", progress=10)

            self.update_state(state="PROGRESS", meta={"current": 20, "total": 100, "status": "Fetching vendor data..."})
            data = await _fetch_debrief_data(rfp_id, response_id)

            self.update_state(state="PROGRESS", meta={"current": 40, "total": 100, "status": "Writing debrief narrative..."})
            narrative = await _generate_debrief_narrative(data)

            self.update_state(state="PROGRESS", meta={"current": 65, "total": 100, "status": "Rendering PDF..."})
            html_content = _generate_debrief_html(data, narrative)
            pdf_bytes    = HTML(string=html_content).write_pdf()

            self.update_state(state="PROGRESS", meta={"current": 85, "total": 100, "status": "Uploading..."})
            vendor_slug = (data["vendor"].name if data["vendor"] else "vendor").lower().replace(" ", "_")[:30]
            s3_client   = _get_s3_client()
            file_key    = f"reports/{rfp_id}/{report_id}_debrief_{vendor_slug}.pdf"
            _ensure_bucket(s3_client, settings.S3_BUCKET)
            s3_client.put_object(
                Bucket=settings.S3_BUCKET, Key=file_key,
                Body=pdf_bytes, ContentType="application/pdf",
                Metadata={"report_id": report_id, "rfp_id": rfp_id,
                          "response_id": response_id, "report_type": "vendor_debrief"},
            )

            s3_path = f"s3://{settings.S3_BUCKET}/{file_key}"
            await _update_report_success(report_id, file_path=s3_path, file_size=len(pdf_bytes))
            await _update_task_status(
                task_id, "done", progress=100,
                result={"report_id": report_id, "file_path": s3_path,
                        "vendor_name": data["vendor"].name if data["vendor"] else "unknown"},
            )
            logger.info("[Task %s] Vendor debrief complete (%d bytes)", task_id, len(pdf_bytes))
            return {"report_id": report_id, "status": "completed", "file_path": s3_path}

        except Exception as exc:
            logger.error("[Task %s] Vendor debrief failed: %s", task_id, exc, exc_info=True)
            await _update_report_error(report_id, str(exc))
            await _update_task_status(task_id, "error", error=str(exc))
            raise
        finally:
            await _eng.dispose()

    return asyncio.run(_main())
