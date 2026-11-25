"""
app/routes/evaluator.py
────────────────────────────────────────────────────────────────────────────
Endpoints used by assigned evaluators during Phase 3 (human review).

ROLE VISIBILITY RULES
──────────────────────
Each evaluator only sees criteria whose owner_role matches their assigned
evaluator_role for this RFP (or criteria with no owner_role = visible to all).
They CANNOT see other roles' scores until the RFP reaches consensus state.

ENDPOINTS
──────────
GET  /api/v1/rfp/{rfp_id}/evaluator/dashboard
     — My assigned criteria + current AI scores + my overrides so far.
       Filters by the requesting user's evaluator_role for this RFP.

POST /api/v1/rfp/{rfp_id}/evaluator/submit
     — Irreversibly mark this evaluator's review as complete.
       Freezes is_submitted=True on their RFPAssignment and all their
       RequirementScore rows. When ALL evaluators submit, the RFP
       auto-transitions to human_eval_in_progress → consensus if all done.

POST /api/v1/rfp/{rfp_id}/vendor/{response_id}/vote
     — Business rep 👍/👎/🤔 vote on a vendor. Upserts on (rfp_id, response_id, user_id).

GET  /api/v1/rfp/{rfp_id}/votes
     — All business votes for an RFP (admin only during consensus).
"""

import uuid
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func

from app.core.auth import get_current_user, require_role
from app.db.session import AsyncSessionLocal
from app.models.db import (
    AuditLog, BusinessVote, EvaluationCriterion, RFPAssignment,
    RFPPackage, RFPStatus, Requirement, RequirementScore,
    VendorResponse, VendorScore, Vendor,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["evaluator"])

# Evaluator roles that get filtered views (admin sees everything)
EVALUATOR_ROLES = {
    "technical_evaluator",
    "financial_evaluator",
    "operations_evaluator",
    "legal_compliance",
}


# ── Schemas ───────────────────────────────────────────────────────────────────

class SubmitBody(BaseModel):
    confirm: bool = Field(..., description="Must be true — this action is irreversible")


class VoteBody(BaseModel):
    vote:    str            = Field(..., pattern="^(yes|no|maybe)$")
    comment: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_assignment(db, rfp_id: str, user_id: str) -> RFPAssignment:
    row = (
        await db.execute(
            select(RFPAssignment).where(
                RFPAssignment.rfp_id  == rfp_id,
                RFPAssignment.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(403, "You are not assigned to this RFP")
    return row


async def _get_rfp(db, rfp_id: str, tenant_id: str) -> RFPPackage:
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


# ── Evaluator dashboard ───────────────────────────────────────────────────────

@router.get("/rfp/{rfp_id}/evaluator/dashboard")
async def evaluator_dashboard(
    rfp_id:  str,
    payload: dict = Depends(get_current_user),
):
    """
    Role-filtered view of AI scores for the requesting evaluator.

    Returns only criteria whose owner_role matches the evaluator's assigned
    role for this RFP. Each criterion shows AI scores + this evaluator's
    overrides for every vendor.

    Evaluators CANNOT see:
    - Other roles' scores (until consensus)
    - Criteria outside their category
    """
    user_id   = payload["sub"]
    tenant_id = payload["tenant_id"]

    async with AsyncSessionLocal() as db:
        rfp        = await _get_rfp(db, rfp_id, tenant_id)
        assignment = await _get_assignment(db, rfp_id, user_id)
        my_role    = assignment.evaluator_role

        # Criteria visible to this role: owner_role == my_role OR owner_role is null
        criteria = (
            await db.execute(
                select(EvaluationCriterion).where(
                    EvaluationCriterion.rfp_id == rfp_id,
                    EvaluationCriterion.owner_role.in_([my_role, None]),
                )
            )
        ).scalars().all()

        criterion_ids = [str(c.criterion_id) for c in criteria]

        # All vendor responses for this RFP
        responses = (
            await db.execute(
                select(VendorResponse, Vendor)
                .join(Vendor, Vendor.vendor_id == VendorResponse.vendor_id)
                .where(VendorResponse.rfp_id == rfp_id)
            )
        ).all()

        # Scores for my criteria only, produced by AI or by me
        scores = (
            await db.execute(
                select(RequirementScore, Requirement)
                .join(Requirement, Requirement.req_id == RequirementScore.req_id)
                .where(
                    Requirement.rfp_id == rfp_id,
                    RequirementScore.criterion_id.in_(criterion_ids),
                )
            )
        ).all()

        # Build score lookup: (response_id, criterion_id) → score
        score_map: dict[tuple, RequirementScore] = {}
        for s, _ in scores:
            score_map[(str(s.response_id), str(s.criterion_id))] = s

        vendors_out = []
        for resp, vendor in responses:
            per_criterion = []
            for c in criteria:
                s = score_map.get((str(resp.response_id), str(c.criterion_id)))
                per_criterion.append({
                    "criterion_id":   str(c.criterion_id),
                    "criterion_label": c.label,
                    "criterion_type": c.criterion_type or "scored",
                    "max_score":      c.max_score or 5,
                    "ai_score":       s.ai_score if s else None,
                    "human_score":    s.human_score if s else None,
                    "final_score":    _final(s) if s else None,
                    "confidence_flag": s.confidence_flag if s else False,
                    "justification":  s.justification if s else None,
                    "is_submitted":   s.is_submitted if s else False,
                    "score_id":       str(s.score_id) if s else None,
                })
            vendors_out.append({
                "response_id": str(resp.response_id),
                "vendor_name": vendor.name,
                "scores":      per_criterion,
            })

        return {
            "rfp_id":        rfp_id,
            "rfp_title":     rfp.title,
            "rfp_status":    rfp.status,
            "my_role":       my_role,
            "is_submitted":  assignment.is_submitted,
            "submitted_at":  assignment.submitted_at.isoformat() if assignment.submitted_at else None,
            "criteria_count": len(criteria),
            "vendor_count":  len(responses),
            "vendors":       vendors_out,
        }


# ── Evaluator submit (irreversible) ───────────────────────────────────────────

@router.post("/rfp/{rfp_id}/evaluator/submit")
async def evaluator_submit(
    rfp_id:  str,
    body:    SubmitBody,
    payload: dict = Depends(get_current_user),
):
    """
    Mark this evaluator's review complete. IRREVERSIBLE.

    - Freezes is_submitted=True on the RFPAssignment row.
    - Marks all this evaluator's RequirementScore rows as is_submitted=True.
    - Checks if ALL evaluators for this RFP have now submitted.
    - If yes and RFP is in human_eval_in_progress → auto-transitions to consensus.
    """
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to submit. This action is irreversible.")

    user_id   = payload["sub"]
    tenant_id = payload["tenant_id"]
    now       = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        rfp        = await _get_rfp(db, rfp_id, tenant_id)
        assignment = await _get_assignment(db, rfp_id, user_id)

        if assignment.is_submitted:
            raise HTTPException(409, "You have already submitted your evaluation for this RFP")

        if rfp.status not in (
            RFPStatus.AI_EVAL_COMPLETE,
            RFPStatus.HUMAN_EVAL_IN_PROGRESS,
            "ai_eval_complete",
            "human_eval_in_progress",
        ):
            raise HTTPException(
                400,
                f"Cannot submit in current RFP state '{rfp.status}'. "
                "Wait for AI evaluation to complete.",
            )

        # Mark assignment as submitted
        assignment.is_submitted = True
        assignment.submitted_at = now

        # Freeze all score rows for this evaluator's criteria
        my_role   = assignment.evaluator_role
        criteria  = (
            await db.execute(
                select(EvaluationCriterion).where(
                    EvaluationCriterion.rfp_id == rfp_id,
                    EvaluationCriterion.owner_role.in_([my_role, None]),
                )
            )
        ).scalars().all()
        criterion_ids = [str(c.criterion_id) for c in criteria]

        scores = (
            await db.execute(
                select(RequirementScore).where(
                    RequirementScore.criterion_id.in_(criterion_ids),
                )
            )
        ).scalars().all()
        for s in scores:
            s.is_submitted  = True
            s.submitted_at  = now

        # Auto-transition RFP to human_eval_in_progress if still in ai_eval_complete
        if rfp.status in ("ai_eval_complete", RFPStatus.AI_EVAL_COMPLETE):
            rfp.status = "human_eval_in_progress"

        # Check if ALL evaluators have now submitted
        all_assignments = (
            await db.execute(
                select(RFPAssignment).where(RFPAssignment.rfp_id == rfp_id)
            )
        ).scalars().all()

        all_submitted    = all(a.is_submitted for a in all_assignments)
        evaluator_count  = len(all_assignments)
        submitted_count  = sum(1 for a in all_assignments if a.is_submitted)

        # If all done → auto-transition to consensus
        if all_submitted and rfp.status in ("human_eval_in_progress", RFPStatus.HUMAN_EVAL_IN_PROGRESS):
            rfp.status = "consensus"
            logger.info("RFP %s auto-transitioned to consensus — all evaluators submitted", rfp_id)

        # Audit entry
        db.add(AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id,
            entity_type="rfp_assignment",
            entity_id=str(assignment.assignment_id),
            action="evaluator_submitted",
            actor=user_id,
            actor_user_id=user_id,
            details={
                "evaluator_role":  my_role,
                "submitted_at":    now.isoformat(),
                "all_submitted":   all_submitted,
                "submitted_count": submitted_count,
                "evaluator_count": evaluator_count,
                "rfp_new_status":  rfp.status,
            },
        ))
        await db.commit()

    return {
        "status":           "submitted",
        "rfp_id":           rfp_id,
        "evaluator_role":   my_role,
        "submitted_at":     now.isoformat(),
        "all_submitted":    all_submitted,
        "submitted_count":  submitted_count,
        "evaluator_count":  evaluator_count,
        "rfp_status":       rfp.status,
        "message": (
            "All evaluators have submitted. RFP moved to consensus."
            if all_submitted
            else f"{submitted_count}/{evaluator_count} evaluators have submitted."
        ),
    }


# ── Business rep vote ─────────────────────────────────────────────────────────

@router.post("/rfp/{rfp_id}/vendor/{response_id}/vote")
async def submit_vote(
    rfp_id:      str,
    response_id: str,
    body:        VoteBody,
    payload:     dict = Depends(require_role("business_rep", "admin")),
):
    """
    Business representative vote: yes / no / maybe.
    Upserts — calling again updates the vote.
    Business reps see vendor name and summary only; no scores visible.
    """
    user_id   = payload["sub"]
    tenant_id = payload["tenant_id"]
    now       = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        rfp = await _get_rfp(db, rfp_id, tenant_id)

        # Confirm response exists
        resp = (
            await db.execute(
                select(VendorResponse).where(
                    VendorResponse.response_id == response_id,
                    VendorResponse.rfp_id      == rfp_id,
                )
            )
        ).scalar_one_or_none()
        if not resp:
            raise HTTPException(404, "Vendor response not found")

        # Upsert
        existing = (
            await db.execute(
                select(BusinessVote).where(
                    BusinessVote.rfp_id      == rfp_id,
                    BusinessVote.response_id == response_id,
                    BusinessVote.user_id     == user_id,
                )
            )
        ).scalar_one_or_none()

        if existing:
            old_vote     = existing.vote
            existing.vote       = body.vote
            existing.comment    = body.comment
            existing.updated_at = now
            action = "vote_updated"
        else:
            old_vote = None
            db.add(BusinessVote(
                vote_id=str(uuid.uuid4()),
                rfp_id=rfp_id,
                response_id=response_id,
                user_id=user_id,
                tenant_id=tenant_id,
                vote=body.vote,
                comment=body.comment,
                voted_at=now,
                updated_at=now,
            ))
            action = "vote_cast"

        db.add(AuditLog(
            tenant_id=tenant_id,
            rfp_id=rfp_id,
            entity_type="business_vote",
            entity_id=response_id,
            action=action,
            actor=user_id,
            actor_user_id=user_id,
            old_value={"vote": old_vote} if old_vote else None,
            new_value={"vote": body.vote, "comment": body.comment},
        ))
        await db.commit()

    return {
        "status":      action,
        "rfp_id":      rfp_id,
        "response_id": response_id,
        "vote":        body.vote,
        "voted_at":    now.isoformat(),
    }


@router.get("/rfp/{rfp_id}/votes")
async def get_votes(
    rfp_id:  str,
    payload: dict = Depends(require_role("admin")),
):
    """All business votes for an RFP. Admin only."""
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        await _get_rfp(db, rfp_id, tenant_id)
        rows = (
            await db.execute(
                select(BusinessVote, Vendor, VendorResponse)
                .join(VendorResponse, VendorResponse.response_id == BusinessVote.response_id)
                .join(Vendor, Vendor.vendor_id == VendorResponse.vendor_id)
                .where(BusinessVote.rfp_id == rfp_id)
                .order_by(Vendor.name, BusinessVote.voted_at)
            )
        ).all()

    # Aggregate per vendor
    by_vendor: dict[str, dict] = {}
    for vote, vendor, resp in rows:
        vid = str(vendor.vendor_id)
        if vid not in by_vendor:
            by_vendor[vid] = {
                "vendor_name": vendor.name,
                "response_id": str(resp.response_id),
                "yes": 0, "no": 0, "maybe": 0,
                "votes": [],
            }
        by_vendor[vid][vote.vote] += 1
        by_vendor[vid]["votes"].append({
            "user_id":  str(vote.user_id),
            "vote":     vote.vote,
            "comment":  vote.comment,
            "voted_at": vote.voted_at.isoformat(),
        })

    return {
        "rfp_id":  rfp_id,
        "vendors": list(by_vendor.values()),
    }
