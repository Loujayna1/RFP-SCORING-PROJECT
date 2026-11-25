"""
app/routes/consensus.py
────────────────────────────────────────────────────────────────────────────
Phase 4 endpoints — consensus meeting, winner declaration, and final lock.

Only available once RFP is in `consensus` state (all evaluators submitted).
All endpoints require the `admin` role.

ENDPOINTS
──────────
GET  /api/v1/rfp/{rfp_id}/consensus
     — Full cross-evaluator score reveal. First time all scores are visible
       side-by-side. Flags high-variance scores.

PATCH /api/v1/rfp/{rfp_id}/consensus/override
     — Admin can override any final score during the meeting with a reason.

POST /api/v1/rfp/{rfp_id}/consensus/notes
     — Record meeting notes (not a transition — just a text field update).

POST /api/v1/rfp/{rfp_id}/declare-winner
     — Declare a winning vendor. Required before locking.
       Accepts optional tie_breaker_notes.

POST /api/v1/rfp/{rfp_id}/lock
     — Terminal lock. No further changes. Triggers report generation.
       Requires winner already declared.
"""

import uuid
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func

from app.core.auth import require_role
from app.db.session import AsyncSessionLocal
from app.services.evaluation import recompute_total_from_rows
from app.models.db import (
    AuditLog, EvaluationCriterion, RFPAssignment, RFPPackage,
    Requirement, RequirementScore, Vendor, VendorResponse, VendorScore,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["consensus"])

# Variance threshold: AI vs human delta > this fraction of max_score → flagged
VARIANCE_THRESHOLD = 0.20


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConsensusOverrideBody(BaseModel):
    score_id:        str
    final_score:     float
    override_reason: str = Field(min_length=5)


class ConsensusNotesBody(BaseModel):
    notes: str


class DeclareWinnerBody(BaseModel):
    vendor_id:         str
    reason:            str = Field(min_length=10)
    tie_breaker_notes: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _require_consensus_state(rfp: RFPPackage):
    if rfp.status not in ("consensus", "locked"):
        raise HTTPException(
            400,
            f"Consensus view is only available in 'consensus' or 'locked' state. "
            f"Current: '{rfp.status}'.",
        )


async def _get_rfp_admin(db, rfp_id: str, tenant_id: str) -> RFPPackage:
    rfp = (
        await db.execute(
            select(RFPPackage).where(
                RFPPackage.rfp_id    == rfp_id,
                RFPPackage.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not rfp:
        raise HTTPException(404, "RFP not found")
    return rfp


def _final(score: RequirementScore) -> float:
    return score.human_score if score.human_score is not None else score.raw_score


# ── Consensus view ────────────────────────────────────────────────────────────

@router.get("/rfp/{rfp_id}/consensus")
async def get_consensus(
    rfp_id:  str,
    payload: dict = Depends(require_role("admin")),
):
    """
    Full cross-evaluator score reveal — only available in consensus state.

    Returns for each vendor:
      - total weighted score
      - per-criterion breakdown with ALL evaluator scores side by side
      - variance flags (AI vs human delta > VARIANCE_THRESHOLD)
      - business votes summary
    """
    tenant_id = payload["tenant_id"]

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_admin(db, rfp_id, tenant_id)
        await _require_consensus_state(rfp)

        criteria = (
            await db.execute(
                select(EvaluationCriterion)
                .where(EvaluationCriterion.rfp_id == rfp_id)
                .order_by(EvaluationCriterion.label)
            )
        ).scalars().all()

        responses = (
            await db.execute(
                select(VendorResponse, Vendor)
                .join(Vendor, Vendor.vendor_id == VendorResponse.vendor_id)
                .where(VendorResponse.rfp_id == rfp_id)
                .order_by(Vendor.name)
            )
        ).all()

        # All scores across all vendors
        all_scores = (
            await db.execute(
                select(RequirementScore, Requirement)
                .join(Requirement, Requirement.req_id == RequirementScore.req_id)
                .where(Requirement.rfp_id == rfp_id)
            )
        ).all()

        # score_map: (response_id, criterion_id) → RequirementScore
        score_map: dict[tuple, RequirementScore] = {}
        for s, _ in all_scores:
            if s.criterion_id:
                score_map[(str(s.response_id), str(s.criterion_id))] = s

        # VendorScore totals
        vendor_totals = (
            await db.execute(
                select(VendorScore)
                .where(VendorScore.rfp_id == rfp_id)
                .order_by(VendorScore.computed_at.desc())
            )
        ).scalars().all()
        totals_map = {str(vs.response_id): vs for vs in vendor_totals}

        # Assignments for evaluator names
        assignments = (
            await db.execute(
                select(RFPAssignment)
                .where(RFPAssignment.rfp_id == rfp_id)
            )
        ).scalars().all()

        vendors_out = []
        for resp, vendor in responses:
            rid         = str(resp.response_id)
            vs          = totals_map.get(rid)
            criteria_out = []
            variance_flags = 0

            for c in criteria:
                s = score_map.get((rid, str(c.criterion_id)))
                ai    = (s.ai_score if s else None)
                human = (s.human_score if s else None)
                final = _final(s) if s else None
                max_s = c.max_score or 5

                # Variance: AI vs human delta
                has_variance = False
                if ai is not None and human is not None:
                    delta = abs(ai - human) / max_s
                    has_variance = delta > VARIANCE_THRESHOLD
                    if has_variance:
                        variance_flags += 1

                criteria_out.append({
                    "criterion_id":    str(c.criterion_id),
                    "criterion_label": c.label,
                    "criterion_type":  c.criterion_type or "scored",
                    "weight":          c.weight,
                    "max_score":       max_s,
                    "owner_role":      c.owner_role,
                    "ai_score":        ai,
                    "human_score":     human,
                    "final_score":     final,
                    "delta":           round(abs(ai - human), 3) if ai is not None and human is not None else None,
                    "has_variance":    has_variance,
                    "justification":   s.justification if s else None,
                    "override_reason": s.override_reason if s else None,
                    "overridden_by":   s.overridden_by if s else None,
                    "score_id":        str(s.score_id) if s else None,
                })

            vendors_out.append({
                "response_id":    rid,
                "vendor_name":    vendor.name,
                "total_score":    vs.total_score if vs else 0.0,
                "section_scores": vs.section_scores if vs else {},
                "variance_flags": variance_flags,
                "criteria":       criteria_out,
            })

        # Sort by total score descending (preliminary ranking)
        vendors_out.sort(key=lambda v: v["total_score"], reverse=True)
        for i, v in enumerate(vendors_out):
            v["preliminary_rank"] = i + 1

        return {
            "rfp_id":           rfp_id,
            "rfp_title":        rfp.title,
            "rfp_status":       rfp.status,
            "winner_vendor_id": str(rfp.winner_vendor_id) if rfp.winner_vendor_id else None,
            "locked_at":        rfp.locked_at.isoformat() if rfp.locked_at else None,
            "consensus_notes":  rfp.consensus_notes,
            "evaluator_count":  len(assignments),
            "all_submitted":    all(a.is_submitted for a in assignments),
            "vendors":          vendors_out,
        }


# ── Admin score override during consensus ─────────────────────────────────────

@router.patch("/rfp/{rfp_id}/consensus/override")
async def consensus_override(
    rfp_id:  str,
    body:    ConsensusOverrideBody,
    payload: dict = Depends(require_role("admin")),
):
    """
    Admin final override during the consensus meeting.
    Can override any score regardless of evaluator role.
    Recalculates VendorScore.total_score immediately.
    """
    tenant_id = payload["tenant_id"]
    admin_id  = payload["sub"]
    now       = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_admin(db, rfp_id, tenant_id)
        if rfp.status == "locked":
            raise HTTPException(400, "RFP is locked — no further changes permitted")

        score = (
            await db.execute(
                select(RequirementScore).where(RequirementScore.score_id == body.score_id)
            )
        ).scalar_one_or_none()
        if not score:
            raise HTTPException(404, "Score not found")

        criterion = None
        if score.criterion_id:
            criterion = (
                await db.execute(
                    select(EvaluationCriterion).where(
                        EvaluationCriterion.criterion_id == score.criterion_id
                    )
                )
            ).scalar_one_or_none()

        max_allowed = float(criterion.max_score) if criterion else 5.0
        if body.final_score < 0 or body.final_score > max_allowed:
            raise HTTPException(422, f"Score out of range [0, {max_allowed}]")

        old_ai    = score.ai_score if score.ai_score is not None else score.raw_score
        old_human = score.human_score

        if score.ai_score is None:
            score.ai_score = score.raw_score
        score.human_score     = body.final_score
        score.override_reason = f"[CONSENSUS ADMIN OVERRIDE] {body.override_reason}"
        score.overridden_by   = admin_id
        score.overridden_at   = now

        # Recalculate total via the single source of truth (aggregate_vendor_score)
        # so consensus overrides stay consistent with the grading pipeline
        # (parent handling, no-evidence exclusion, mandatory gates, normalisation).
        response_id = str(score.response_id)
        all_scores  = (
            await db.execute(
                select(RequirementScore).where(
                    RequirementScore.response_id == response_id
                )
            )
        ).scalars().all()
        all_criteria = (
            await db.execute(
                select(EvaluationCriterion).where(
                    EvaluationCriterion.rfp_id == rfp_id
                )
            )
        ).scalars().all()

        agg   = recompute_total_from_rows(all_scores, all_criteria)
        total = agg["total_score"]

        vs = (
            await db.execute(
                select(VendorScore).where(
                    VendorScore.response_id == response_id,
                    VendorScore.rfp_id      == rfp_id,
                )
            )
        ).scalars().first()
        if vs:
            vs.section_scores = agg["section_scores"]
            vs.total_score    = round(total, 4)
            vs.computed_at    = now

        db.add(AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id,
            entity_type="requirement_score",
            entity_id=body.score_id,
            action="consensus_admin_override",
            actor=admin_id,
            actor_user_id=admin_id,
            old_value={"ai_score": old_ai, "human_score": old_human},
            new_value={"human_score": body.final_score, "reason": body.override_reason},
        ))
        await db.commit()

    return {
        "status":        "overridden",
        "score_id":      body.score_id,
        "final_score":   body.final_score,
        "new_total":     round(total, 4),
        "overridden_at": now.isoformat(),
    }


# ── Consensus notes ───────────────────────────────────────────────────────────

@router.post("/rfp/{rfp_id}/consensus/notes")
async def save_consensus_notes(
    rfp_id:  str,
    body:    ConsensusNotesBody,
    payload: dict = Depends(require_role("admin")),
):
    """Save or update meeting notes. Does not change RFP state."""
    tenant_id = payload["tenant_id"]
    admin_id  = payload["sub"]

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_admin(db, rfp_id, tenant_id)
        if rfp.status == "locked":
            raise HTTPException(400, "RFP is locked")
        rfp.consensus_notes = body.notes
        db.add(AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id, entity_type="rfp_package", entity_id=rfp_id,
            action="consensus_notes_saved", actor=admin_id, actor_user_id=admin_id,
            new_value={"notes_length": len(body.notes)},
        ))
        await db.commit()
    return {"status": "saved", "rfp_id": rfp_id}


# ── Declare winner ────────────────────────────────────────────────────────────

@router.post("/rfp/{rfp_id}/declare-winner")
async def declare_winner(
    rfp_id:  str,
    body:    DeclareWinnerBody,
    payload: dict = Depends(require_role("admin")),
):
    """
    Declare the winning vendor. Required before locking.
    Can be called repeatedly to change the winner before the final lock.
    """
    tenant_id = payload["tenant_id"]
    admin_id  = payload["sub"]
    now       = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_admin(db, rfp_id, tenant_id)
        if rfp.status == "locked":
            raise HTTPException(400, "RFP is locked — cannot change winner")
        await _require_consensus_state(rfp)

        # Verify vendor belongs to this RFP
        vendor = (
            await db.execute(
                select(Vendor).where(
                    Vendor.vendor_id == body.vendor_id,
                    Vendor.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not vendor:
            raise HTTPException(404, "Vendor not found")

        old_winner = str(rfp.winner_vendor_id) if rfp.winner_vendor_id else None

        rfp.winner_vendor_id   = body.vendor_id
        rfp.winner_declared_at = now
        rfp.winner_declared_by = admin_id
        rfp.winner_reason      = body.reason
        rfp.tie_breaker_notes  = body.tie_breaker_notes

        db.add(AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id, entity_type="rfp_package", entity_id=rfp_id,
            action="winner_declared", actor=admin_id, actor_user_id=admin_id,
            old_value={"winner_vendor_id": old_winner},
            new_value={
                "winner_vendor_id": body.vendor_id,
                "vendor_name":      vendor.name,
                "reason":           body.reason,
                "tie_breaker_notes": body.tie_breaker_notes,
            },
        ))
        await db.commit()

    return {
        "status":            "winner_declared",
        "rfp_id":            rfp_id,
        "winner_vendor_id":  body.vendor_id,
        "winner_name":       vendor.name,
        "declared_at":       now.isoformat(),
        "reason":            body.reason,
    }


# ── Final lock ────────────────────────────────────────────────────────────────

@router.post("/rfp/{rfp_id}/lock")
async def lock_rfp(
    rfp_id:  str,
    payload: dict = Depends(require_role("admin")),
):
    """
    Terminal lock. No further changes to scores, winner, or criteria.

    Preconditions:
    - RFP must be in 'consensus' state
    - Winner must have been declared
    - All evaluators must have submitted

    After locking:
    - RFP status → 'locked'
    - locked_at and locked_by set
    - Audit trail sealed (trigger already blocks UPDATE/DELETE)
    - Report generation triggered (Celery task)
    """
    tenant_id = payload["tenant_id"]
    admin_id  = payload["sub"]
    now       = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp_admin(db, rfp_id, tenant_id)

        if rfp.status == "locked":
            return {
                "status":    "already_locked",
                "locked_at": rfp.locked_at.isoformat() if rfp.locked_at else None,
            }

        if rfp.status != "consensus":
            raise HTTPException(
                400,
                f"RFP must be in 'consensus' state to lock. Current: '{rfp.status}'.",
            )

        if not rfp.winner_vendor_id:
            raise HTTPException(400, "Declare a winner before locking the RFP")

        # Verify all evaluators submitted
        assignments = (
            await db.execute(
                select(RFPAssignment).where(RFPAssignment.rfp_id == rfp_id)
            )
        ).scalars().all()
        unsubmitted = [a for a in assignments if not a.is_submitted]
        if unsubmitted:
            raise HTTPException(
                400,
                f"{len(unsubmitted)} evaluator(s) have not submitted. "
                "All must submit before locking.",
            )

        winner = (
            await db.execute(
                select(Vendor).where(Vendor.vendor_id == rfp.winner_vendor_id)
            )
        ).scalar_one_or_none()

        rfp.status    = "locked"
        rfp.locked_at = now
        rfp.locked_by = admin_id

        db.add(AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id, entity_type="rfp_package", entity_id=rfp_id,
            action="rfp_locked", actor=admin_id, actor_user_id=admin_id,
            new_value={
                "locked_at":    now.isoformat(),
                "winner":       winner.name if winner else None,
                "winner_reason": rfp.winner_reason,
            },
        ))
        await db.commit()

    # Queue report generation
    try:
        from app.worker import celery_app, generate_report_pdf
        report_id = str(uuid.uuid4())
        generate_report_pdf.apply_async(
            args=[report_id, rfp_id],
            task_id=report_id,
        )
        logger.info("Queued final report for locked RFP %s", rfp_id)
    except Exception as exc:
        logger.warning("Could not queue report for RFP %s: %s", rfp_id, exc)
        report_id = None

    return {
        "status":           "locked",
        "rfp_id":           rfp_id,
        "locked_at":        now.isoformat(),
        "winner_vendor_id": str(rfp.winner_vendor_id),
        "winner_name":      winner.name if winner else None,
        "report_queued":    report_id is not None,
        "report_id":        report_id,
    }
