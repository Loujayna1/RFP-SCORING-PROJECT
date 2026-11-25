"""
Human-review endpoints — rubric approval and score override.

HOW TO INTEGRATE
────────────────
In backend/app/main.py, add two lines near the top imports:

    from app.routes.human_review import router as human_review_router

Then after `app = FastAPI(...)`:

    app.include_router(human_review_router)

That's it. All routes in this file are automatically registered.

ENDPOINTS ADDED
───────────────
  GET    /api/v1/rfp/{rfp_id}/rubric                         — list criteria for review
  PATCH  /api/v1/rfp/{rfp_id}/rubric                         — bulk-edit criteria (weights, types, labels)
  POST   /api/v1/rfp/{rfp_id}/rubric/lock                    — lock rubric (irreversible per RFP)
  GET    /api/v1/rfp/{rfp_id}/rubric/status                  — is the rubric locked?

  GET    /api/v1/rfp/{rfp_id}/vendor/{response_id}/review    — scores flagged for human review
  PATCH  /api/v1/rfp/{rfp_id}/vendor/{response_id}/review/{score_id}  — override one score
  GET    /api/v1/rfp/{rfp_id}/conflicts                      — cross-vendor divergence report
"""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, update

from app.core.auth import require_role
from app.db.session import AsyncSessionLocal
from app.models import db as models_db
from app.services.evaluation import recompute_total_from_rows

router = APIRouter(prefix="/api/v1", tags=["human-review"])

# Roles allowed to override AI scores (business reps and auditors are read-only)
REVIEWER_ROLES = (
    "admin",
    "technical_evaluator",
    "financial_evaluator",
    "operations_evaluator",
    "legal_compliance",
)

# ── Confidence threshold below which scores are auto-flagged ──────────────────
CONFIDENCE_FLAG_THRESHOLD = 0.75

# ── Score divergence threshold that triggers a conflict alert ─────────────────
# Two reviewers whose scores differ by more than this fraction of max_score
# will appear in GET /conflicts.
CONFLICT_DIVERGENCE_THRESHOLD = 0.20


# ═════════════════════════════════════════════════════════════════════════════
# PYDANTIC SCHEMAS
# ═════════════════════════════════════════════════════════════════════════════

class CriterionEdit(BaseModel):
    """One criterion update sent in PATCH /rubric."""
    criterion_id:    str
    label:           Optional[str]   = None
    section:         Optional[str]   = None
    weight:          Optional[float] = None
    max_score:       Optional[int]   = None
    criterion_type:  Optional[str]   = None  # scored | mandatory | preferred
    # FIX 2 — owner_role controls which evaluator role sees this criterion.
    # NULL = visible to all roles. Pass "" to clear (set to NULL).
    owner_role:      Optional[str]   = None

    @field_validator("criterion_type")
    @classmethod
    def valid_type(cls, v):
        if v is not None and v not in ("scored", "mandatory", "preferred"):
            raise ValueError("criterion_type must be scored | mandatory | preferred")
        return v

    @field_validator("weight")
    @classmethod
    def positive_weight(cls, v):
        if v is not None and v < 0:
            raise ValueError("weight must be >= 0")
        return v


class RubricPatchBody(BaseModel):
    criteria: List[CriterionEdit]
    actor:    str = Field(default="procurement_officer", min_length=1)


class RubricLockBody(BaseModel):
    actor: str = Field(default="procurement_officer", min_length=1)


class ScoreOverrideBody(BaseModel):
    human_score:     float
    override_reason: str = Field(min_length=5, description="Mandatory justification")
    actor:           str = Field(default="procurement_officer", min_length=1)

    @field_validator("override_reason")
    @classmethod
    def reason_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("override_reason cannot be blank")
        return v.strip()


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _serialize_criterion(c: models_db.EvaluationCriterion) -> dict:
    return {
        "criterion_id":     str(c.criterion_id),
        "label":            c.label,
        "section":          c.section,
        "weight":           c.weight,
        "max_score":        c.max_score,
        "criterion_type":   c.criterion_type or "scored",
        "confidence_score": c.confidence_score,
        "extraction_source": c.extraction_source,
        "locked_at":        c.locked_at.isoformat() if c.locked_at else None,
        "approved_by":      c.approved_by,
        "owner_role":       c.owner_role,
        "needs_review":     (
            c.confidence_score is not None
            and c.confidence_score < CONFIDENCE_FLAG_THRESHOLD
            and c.locked_at is None
        ),
    }


def _normalize_weights(criteria: list[models_db.EvaluationCriterion]) -> None:
    """
    Re-normalises weights so scored + preferred criteria sum to 1.0.
    Mandatory criteria have no weight (pass/fail only) and are excluded.
    Mutates the list in place — call before committing to DB.
    """
    scored = [c for c in criteria if (c.criterion_type or "scored") != "mandatory"]
    total = sum(c.weight or 0.0 for c in scored)
    if total <= 0:
        return
    for c in scored:
        c.weight = round((c.weight or 0.0) / total, 6)


async def _record(db, *, action, entity_type, entity_id=None, rfp_id=None, tenant_id=None, actor="system", details=None):
    """Thin wrapper so this module doesn't depend on main.py's record_audit_event."""
    db.add(models_db.AuditLog(
        rfp_id=rfp_id,
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        action=action,
        actor=actor,
        details=details or {},
        created_at=datetime.utcnow(),
    ))


# ═════════════════════════════════════════════════════════════════════════════
# RUBRIC ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/rfp/{rfp_id}/rubric")
async def get_rubric(
    rfp_id: str,
    payload: dict = Depends(require_role()),
):
    """
    Return all evaluation criteria for an RFP, ready for the review UI.

    Response includes:
      - rubric_locked: bool — whether the rubric has been locked
      - locked_at / approved_by — who locked it and when
      - criteria — full list with confidence flags and needs_review hints
      - flagged_count — how many criteria need human attention before locking
    """
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")

        criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion)
                .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
                .order_by(models_db.EvaluationCriterion.section, models_db.EvaluationCriterion.label)
            )
        ).scalars().all()

        serialized = [_serialize_criterion(c) for c in criteria]
        flagged = sum(1 for c in serialized if c["needs_review"])

        return {
            "rfp_id":       rfp_id,
            "rubric_locked": rfp.rubric_locked_at is not None,
            "locked_at":    rfp.rubric_locked_at.isoformat() if rfp.rubric_locked_at else None,
            "approved_by":  rfp.rubric_approved_by,
            "total":        len(serialized),
            "flagged_count": flagged,
            "criteria":     serialized,
        }


@router.patch("/rfp/{rfp_id}/rubric")
async def patch_rubric(
    rfp_id: str,
    body: RubricPatchBody,
    payload: dict = Depends(require_role("admin")),
):
    """
    Bulk-edit evaluation criteria before locking.

    - Rejects edits if the rubric is already locked.
    - Accepts partial updates — only fields provided in each CriterionEdit are changed.
    - After all edits, weights are re-normalised to sum to 1.0 (mandatory criteria excluded).
    - Every change is recorded in audit_log.

    Typical frontend flow:
      1. GET /rubric → officer sees the table
      2. PATCH /rubric → officer edits weights, types, or labels
      3. POST /rubric/lock → officer locks
    """
    body.actor = payload.get("email") or payload["sub"]
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")
        if rfp.rubric_locked_at:
            raise HTTPException(
                409,
                f"Rubric was locked at {rfp.rubric_locked_at.isoformat()} "
                f"by {rfp.rubric_approved_by}. No further edits allowed.",
            )

        updated_ids = []
        changes_detail = []

        for edit in body.criteria:
            row = (
                await db.execute(
                    select(models_db.EvaluationCriterion).where(
                        models_db.EvaluationCriterion.criterion_id == edit.criterion_id,
                        models_db.EvaluationCriterion.rfp_id == rfp_id,
                    )
                )
            ).scalar_one_or_none()
            if not row:
                raise HTTPException(404, f"Criterion {edit.criterion_id} not found for this RFP")

            before = _serialize_criterion(row)
            if edit.label          is not None: row.label          = edit.label
            if edit.section        is not None: row.section        = edit.section
            if edit.weight         is not None: row.weight         = edit.weight
            if edit.max_score      is not None: row.max_score      = edit.max_score
            if edit.criterion_type is not None: row.criterion_type = edit.criterion_type
            # FIX 2 — persist owner_role; empty string means "clear to NULL"
            if edit.owner_role is not None:
                row.owner_role = edit.owner_role if edit.owner_role != "" else None
            after = _serialize_criterion(row)

            updated_ids.append(edit.criterion_id)
            changes_detail.append({"criterion_id": edit.criterion_id, "before": before, "after": after})

        # Re-fetch all criteria and re-normalise weights
        all_criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion).where(
                    models_db.EvaluationCriterion.rfp_id == rfp_id
                )
            )
        ).scalars().all()
        _normalize_weights(all_criteria)

        await _record(
            db,
            action="rubric_edited",
            entity_type="rubric",
            entity_id=rfp_id,
            rfp_id=rfp_id,
            tenant_id=rfp.tenant_id,
            actor=body.actor,
            details={"updated_criteria": updated_ids, "changes": changes_detail},
        )
        await db.commit()

        # Return fresh rubric state
        refreshed = (
            await db.execute(
                select(models_db.EvaluationCriterion)
                .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
                .order_by(models_db.EvaluationCriterion.section, models_db.EvaluationCriterion.label)
            )
        ).scalars().all()
        return {
            "status":   "updated",
            "rfp_id":   rfp_id,
            "criteria": [_serialize_criterion(c) for c in refreshed],
        }


@router.get("/rfp/{rfp_id}/rubric/status")
async def rubric_status(
    rfp_id: str,
    payload: dict = Depends(require_role()),
):
    """Quick check — is the rubric locked? Used by the pipeline before writing criteria."""
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")
        return {
            "rfp_id":       rfp_id,
            "rubric_locked": rfp.rubric_locked_at is not None,
            "locked_at":    rfp.rubric_locked_at.isoformat() if rfp.rubric_locked_at else None,
            "approved_by":  rfp.rubric_approved_by,
        }


@router.post("/rfp/{rfp_id}/rubric/lock")
async def lock_rubric(
    rfp_id: str,
    body: RubricLockBody,
    payload: dict = Depends(require_role("admin")),
):
    """
    Irreversibly lock the rubric for this RFP.

    After locking:
      - PATCH /rubric returns 409.
      - The AI pipeline will not overwrite criterion weights or types.
      - All vendor proposals will be scored against this exact rubric.

    Validation before locking:
      - At least one criterion must exist.
      - Weight sum of non-mandatory criteria must be > 0.
      - No criteria may have been deleted after scoring started (checked via
        requirement_score rows).
    """
    body.actor = payload.get("email") or payload["sub"]
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")

        if rfp.rubric_locked_at:
            return {
                "status":     "already_locked",
                "locked_at":  rfp.rubric_locked_at.isoformat(),
                "approved_by": rfp.rubric_approved_by,
            }

        criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion)
                .where(models_db.EvaluationCriterion.rfp_id == rfp_id)
            )
        ).scalars().all()

        if not criteria:
            raise HTTPException(
                422,
                "Cannot lock an empty rubric. "
                "Wait for the extraction pipeline to finish or add criteria manually.",
            )

        scored_weight = sum(
            (c.weight or 0.0)
            for c in criteria
            if (c.criterion_type or "scored") != "mandatory"
        )
        if scored_weight <= 0:
            raise HTTPException(
                422,
                "Total weight of scored criteria is 0. "
                "Add at least one scored criterion with weight > 0 before locking.",
            )

        now = datetime.utcnow()

        # Lock at package level
        rfp.rubric_locked_at   = now
        rfp.rubric_approved_by = body.actor

        # Lock each criterion row (for per-row queries)
        for c in criteria:
            c.locked_at   = now
            c.approved_by = body.actor

        flagged = sum(
            1 for c in criteria
            if c.confidence_score is not None and c.confidence_score < CONFIDENCE_FLAG_THRESHOLD
        )

        await _record(
            db,
            action="rubric_locked",
            entity_type="rubric",
            entity_id=rfp_id,
            rfp_id=rfp_id,
            tenant_id=rfp.tenant_id,
            actor=body.actor,
            details={
                "criteria_count":  len(criteria),
                "flagged_at_lock": flagged,
                "scored_weight":   round(scored_weight, 4),
            },
        )
        await db.commit()

        return {
            "status":          "locked",
            "rfp_id":          rfp_id,
            "locked_at":       now.isoformat(),
            "approved_by":     body.actor,
            "criteria_count":  len(criteria),
            "flagged_at_lock": flagged,
        }


# ═════════════════════════════════════════════════════════════════════════════
# SCORE REVIEW ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/rfp/{rfp_id}/vendor/{response_id}/review")
async def get_flagged_scores(
    rfp_id:      str,
    response_id: str,
    all_scores:  bool = Query(False, description="Return all scores, not just flagged ones"),
    payload: dict = Depends(require_role()),
):
    """
    Return requirement scores that need human review.

    By default returns only scores where confidence_flag=True (AI was uncertain).
    Pass ?all_scores=true to see every score for a vendor alongside the flag.

    Each row includes:
      - ai_score: what the model said
      - human_score: override if set (null otherwise)
      - final_score: COALESCE(human_score, raw_score) — used in aggregation
      - confidence_flag: whether this row needs review
      - override_reason / overridden_by / overridden_at — audit trail
    """
    async with AsyncSessionLocal() as db:
        response = (
            await db.execute(
                select(models_db.VendorResponse).where(
                    models_db.VendorResponse.response_id == response_id,
                    models_db.VendorResponse.rfp_id == rfp_id,
                    models_db.VendorResponse.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not response:
            raise HTTPException(404, "Vendor response not found")

        vendor = (
            await db.execute(
                select(models_db.Vendor).where(models_db.Vendor.vendor_id == response.vendor_id)
            )
        ).scalar_one_or_none()

        query = (
            select(models_db.RequirementScore, models_db.Requirement)
            .join(models_db.Requirement, models_db.RequirementScore.req_id == models_db.Requirement.req_id)
            .where(models_db.RequirementScore.response_id == response_id)
        )
        if not all_scores:
            query = query.where(models_db.RequirementScore.confidence_flag == True)
        query = query.order_by(models_db.RequirementScore.scored_at.desc())

        rows = (await db.execute(query)).all()

        scores = []
        for score_row, req_row in rows:
            ai    = score_row.ai_score if score_row.ai_score is not None else score_row.raw_score
            human = score_row.human_score
            final = human if human is not None else score_row.raw_score
            scores.append({
                "score_id":        str(score_row.score_id),
                "req_id":          str(score_row.req_id),
                "req_code":        req_row.req_code,
                "description":     req_row.description,
                "category":        req_row.category,
                "is_mandatory":    req_row.is_mandatory,
                "ai_score":        ai,
                "human_score":     human,
                "final_score":     final,
                "raw_score":       score_row.raw_score,
                "compliance_level": score_row.compliance_level,
                "justification":   score_row.justification,
                "confidence_flag": score_row.confidence_flag,
                "override_reason": score_row.override_reason,
                "overridden_by":   score_row.overridden_by,
                "overridden_at":   score_row.overridden_at.isoformat() if score_row.overridden_at else None,
                "scored_at":       score_row.scored_at.isoformat() if score_row.scored_at else None,
            })

        return {
            "rfp_id":      rfp_id,
            "response_id": response_id,
            "vendor_name": vendor.name if vendor else None,
            "total":       len(scores),
            "scores":      scores,
        }


@router.patch("/rfp/{rfp_id}/vendor/{response_id}/review/{score_id}")
async def override_score(
    rfp_id:      str,
    response_id: str,
    score_id:    str,
    body:        ScoreOverrideBody,
    payload: dict = Depends(require_role(*REVIEWER_ROLES)),
):
    """
    Human override for a single RequirementScore.

    Rules:
      - override_reason is mandatory and must be at least 5 characters.
      - human_score must be within [0, max_score] of the linked criterion.
      - If the criterion has criterion_type=mandatory, human_score can only be 0 or max_score.
      - The AI score is preserved in ai_score for comparison.
      - Every override is recorded in audit_log.
      - After override, VendorScore.total_score is recalculated immediately
        so the ranking dashboard stays current.
    """
    body.actor = payload.get("email") or payload["sub"]
    async with AsyncSessionLocal() as db:
        # Fetch RFP to get tenant_id for audit logging
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")

        score_row = (
            await db.execute(
                select(models_db.RequirementScore).where(
                    models_db.RequirementScore.score_id == score_id,
                    models_db.RequirementScore.response_id == response_id,
                )
            )
        ).scalar_one_or_none()
        if not score_row:
            raise HTTPException(404, "Score not found")

        # Validate against criterion bounds
        criterion = None
        if score_row.criterion_id:
            criterion = (
                await db.execute(
                    select(models_db.EvaluationCriterion).where(
                        models_db.EvaluationCriterion.criterion_id == score_row.criterion_id
                    )
                )
            ).scalar_one_or_none()

        max_allowed = float(criterion.max_score) if criterion else 5.0
        if body.human_score < 0 or body.human_score > max_allowed:
            raise HTTPException(
                422,
                f"human_score {body.human_score} is out of range [0, {max_allowed}]",
            )

        if criterion and (criterion.criterion_type or "scored") == "mandatory":
            if body.human_score not in (0.0, max_allowed):
                raise HTTPException(
                    422,
                    f"Mandatory criterion — score must be 0 (fail) or {max_allowed} (pass).",
                )

        # Preserve AI score on first override
        if score_row.ai_score is None:
            score_row.ai_score = score_row.raw_score

        previous_human = score_row.human_score
        now = datetime.utcnow()

        score_row.human_score     = body.human_score
        score_row.override_reason = body.override_reason
        score_row.overridden_by   = body.actor
        score_row.overridden_at   = now
        score_row.confidence_flag = False  # reviewed — clear the flag

        # ── Recalculate VendorScore.total_score ───────────────────────────────
        # Single source of truth: the same aggregation the grading pipeline uses
        # (aggregate_vendor_score), so an override never diverges from the graded
        # total — correct parent handling, no-evidence exclusion, mandatory
        # gates, and weight normalisation.
        all_scores = (
            await db.execute(
                select(models_db.RequirementScore).where(
                    models_db.RequirementScore.response_id == response_id
                )
            )
        ).scalars().all()
        all_criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion).where(
                    models_db.EvaluationCriterion.rfp_id == rfp_id
                )
            )
        ).scalars().all()

        agg   = recompute_total_from_rows(all_scores, all_criteria)
        total = agg["total_score"]

        vendor_score_row = (
            await db.execute(
                select(models_db.VendorScore).where(
                    models_db.VendorScore.response_id == response_id,
                    models_db.VendorScore.rfp_id == rfp_id,
                )
            )
        ).scalars().first()

        if vendor_score_row:
            vendor_score_row.section_scores = agg["section_scores"]
            vendor_score_row.total_score    = round(total, 4)
            vendor_score_row.computed_at    = now

        await _record(
            db,
            action="score_overridden",
            entity_type="requirement_score",
            entity_id=score_id,
            rfp_id=rfp_id,
            tenant_id=rfp.tenant_id,
            actor=body.actor,
            details={
                "response_id":      response_id,
                "req_id":           str(score_row.req_id),
                "ai_score":         score_row.ai_score,
                "previous_human":   previous_human,
                "new_human_score":  body.human_score,
                "override_reason":  body.override_reason,
                "new_total_score":  round(total, 4),
            },
        )
        await db.commit()

        return {
            "status":          "overridden",
            "score_id":        score_id,
            "ai_score":        score_row.ai_score,
            "human_score":     body.human_score,
            "final_score":     body.human_score,
            "override_reason": body.override_reason,
            "overridden_by":   body.actor,
            "overridden_at":   now.isoformat(),
            "new_total_score": round(total, 4),
        }


# ═════════════════════════════════════════════════════════════════════════════
# CONFLICT DETECTION
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/rfp/{rfp_id}/conflicts")
async def get_conflicts(
    rfp_id:    str,
    threshold: float = Query(
        CONFLICT_DIVERGENCE_THRESHOLD,
        description="Fraction of max_score difference that triggers a conflict alert (default 0.20)",
        ge=0.0,
        le=1.0,
    ),
    payload: dict = Depends(require_role()),
):
    """
    Cross-vendor score divergence report.

    For each requirement that has been scored for more than one vendor,
    checks whether the SPREAD between the highest and lowest score
    (as a fraction of max_score) exceeds the threshold.

    A conflict here does NOT mean two reviewers scored the same vendor
    differently — it means the same requirement was scored very differently
    across vendors, which may indicate inconsistent evaluation criteria or
    a criterion that needs human clarification.

    Response:
      conflicts — list of requirements where spread > threshold
      clean     — count of requirements within acceptable spread
    """
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                select(models_db.RFPPackage).where(
                    models_db.RFPPackage.rfp_id == rfp_id,
                    models_db.RFPPackage.tenant_id == payload["tenant_id"],
                )
            )
        ).scalar_one_or_none()
        if not rfp:
            raise HTTPException(404, "RFP not found")

        # All scores for this RFP across all vendors
        rows = (
            await db.execute(
                select(
                    models_db.RequirementScore,
                    models_db.Requirement,
                    models_db.VendorResponse,
                    models_db.Vendor,
                )
                .join(models_db.Requirement,     models_db.RequirementScore.req_id      == models_db.Requirement.req_id)
                .join(models_db.VendorResponse,  models_db.RequirementScore.response_id == models_db.VendorResponse.response_id)
                .join(models_db.Vendor,          models_db.VendorResponse.vendor_id     == models_db.Vendor.vendor_id)
                .where(models_db.Requirement.rfp_id == rfp_id)
                .order_by(models_db.Requirement.req_id)
            )
        ).all()

        # Group scores by requirement
        by_req: dict[str, dict] = {}
        for score_row, req_row, resp_row, vendor_row in rows:
            rid = str(req_row.req_id)
            if rid not in by_req:
                by_req[rid] = {
                    "req_id":      rid,
                    "req_code":    req_row.req_code,
                    "description": req_row.description,
                    "criterion_id": str(score_row.criterion_id) if score_row.criterion_id else None,
                    "vendor_scores": [],
                }
            final = score_row.human_score if score_row.human_score is not None else score_row.raw_score
            by_req[rid]["vendor_scores"].append({
                "vendor_name":  vendor_row.name,
                "response_id":  str(resp_row.response_id),
                "final_score":  final,
                "is_overridden": score_row.human_score is not None,
            })

        # Get max_score per criterion for normalisation
        criteria = (
            await db.execute(
                select(models_db.EvaluationCriterion).where(
                    models_db.EvaluationCriterion.rfp_id == rfp_id
                )
            )
        ).scalars().all()
        # Use 5 as fallback if no criterion linked
        criterion_max_scores = {str(c.criterion_id): (c.max_score or 5) for c in criteria}
        default_max = max((c.max_score or 5 for c in criteria), default=5)

        conflicts = []
        clean_count = 0

        for rid, data in by_req.items():
            vs = data["vendor_scores"]
            if len(vs) < 2:
                # Only one vendor scored — no comparison possible
                continue

            scores_only = [v["final_score"] for v in vs if v["final_score"] is not None]
            if not scores_only:
                continue

            # Determine criterion specific max score, fall back to default_max
            crit_id = data.get("criterion_id")
            max_score = criterion_max_scores.get(crit_id) if crit_id else default_max

            spread    = max(scores_only) - min(scores_only)
            spread_pct = round(spread / max_score, 4)

            # Build data representation without internal fields
            req_data = {
                "req_id":      data["req_id"],
                "req_code":    data["req_code"],
                "description": data["description"],
                "vendor_scores": data["vendor_scores"],
            }

            if spread_pct > threshold:
                conflicts.append({
                    **req_data,
                    "spread":      round(spread, 4),
                    "spread_pct":  spread_pct,
                    "max_score":   max_score,
                })
            else:
                clean_count += 1

        conflicts.sort(key=lambda x: x["spread_pct"], reverse=True)

        return {
            "rfp_id":          rfp_id,
            "threshold":       threshold,
            "conflict_count":  len(conflicts),
            "clean_count":     clean_count,
            "conflicts":       conflicts,
        }
