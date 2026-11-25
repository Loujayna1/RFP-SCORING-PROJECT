"""
app/routes/tenant.py
────────────────────────────────────────────────────────────────────────────
Tenant and user management API.

ENDPOINTS
──────────
POST /api/v1/tenants/register          — public: create new tenant + admin user
GET  /api/v1/tenants/me                — current tenant info + stats
GET  /api/v1/users                     — list users in tenant (admin)
POST /api/v1/users/invite              — invite a new user by email (admin)
DELETE /api/v1/users/{user_id}         — deactivate a user (admin)
GET  /api/v1/rfp/{rfp_id}/assignments  — list evaluators assigned to an RFP
POST /api/v1/rfp/{rfp_id}/assignments  — assign evaluator to RFP (admin)
DELETE /api/v1/rfp/{rfp_id}/assignments/{assignment_id} — remove assignment
POST /api/v1/rfp/{rfp_id}/transition   — advance RFP state machine (admin)
"""

import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from app.core.auth import get_current_user, require_role
from app.db.session import AsyncSessionLocal
from app.models.db import (
    RFPAssignment, RFPPackage, Tenant, User,
    UserRole, UserStatus, VALID_TRANSITIONS,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tenants"])
_pwd   = CryptContext(schemes=["bcrypt"], deprecated="auto")

INVITE_EXPIRY_HOURS = 48


# ── Schemas ───────────────────────────────────────────────────────────────────

class TenantRegisterRequest(BaseModel):
    company_name:   str
    admin_email:    str
    admin_password: str
    plan:           Optional[str] = "pro"  # internal tool; not exposed in signup UI


class InviteRequest(BaseModel):
    email:          str
    role:           str
    display_name:   Optional[str] = None


class AssignRequest(BaseModel):
    user_id:        str
    evaluator_role: str


class TransitionRequest(BaseModel):
    to: str
    reason: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:80] + "-" + secrets.token_hex(3)


def _validate_role(role: str):
    valid = {r.value for r in UserRole}
    if role not in valid:
        raise HTTPException(400, f"Invalid role '{role}'. Valid: {', '.join(sorted(valid))}")


# ── Tenant registration (public) ──────────────────────────────────────────────

@router.post("/api/v1/tenants/register", status_code=201)
async def register_tenant(body: TenantRegisterRequest):
    """
    Creates a new tenant and its first admin user.
    No authentication required — this is the public signup endpoint.
    """
    slug = _slugify(body.company_name)
    admin_email = body.admin_email.strip().lower()

    async with AsyncSessionLocal() as db:
        # Check email not already in use
        existing = (
            await db.execute(select(User).where(User.email == admin_email))
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(400, "An account with this email already exists")

        import uuid
        tenant_id = str(uuid.uuid4())
        user_id   = str(uuid.uuid4())

        tenant = Tenant(
            tenant_id=tenant_id,
            name=body.company_name.strip(),
            slug=slug,
            plan=body.plan or "pro",
            status="active",
        )
        db.add(tenant)
        await db.flush()

        admin = User(
            user_id=user_id,
            tenant_id=tenant_id,
            email=admin_email,
            hashed_password=_pwd.hash(body.admin_password),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            accepted_at=datetime.utcnow(),
        )
        db.add(admin)
        await db.commit()

    logger.info("New tenant registered: %s (slug=%s) admin=%s", body.company_name, slug, admin_email)
    return {
        "tenant_id":   tenant_id,
        "slug":        slug,
        "admin_email": admin_email,
        "message":     "Tenant created. Sign in with your admin credentials.",
    }


# ── Tenant info ───────────────────────────────────────────────────────────────

@router.get("/api/v1/tenants/me")
async def get_tenant_me(payload: dict = Depends(get_current_user)):
    tenant_id = payload.get("tenant_id")
    async with AsyncSessionLocal() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "Tenant not found")

        user_count = (
            await db.execute(
                select(func.count(User.user_id)).where(
                    User.tenant_id == tenant_id,
                    User.status    != UserStatus.DEACTIVATED,
                )
            )
        ).scalar_one()

        active_rfp_count = (
            await db.execute(
                select(func.count(RFPPackage.rfp_id)).where(
                    RFPPackage.tenant_id == tenant_id,
                    RFPPackage.status.not_in(["locked", "error"]),
                )
            )
        ).scalar_one()

    return {
        "tenant_id":      str(tenant.tenant_id),
        "name":           tenant.name,
        "slug":           tenant.slug,
        "plan":           tenant.plan,
        "status":         tenant.status,
        "user_count":     user_count,
        "max_users":      tenant.max_users,
        "active_rfps":    active_rfp_count,
        "max_rfps_per_month": tenant.max_rfps_per_month,
        "max_storage_gb": tenant.max_storage_gb,
        "next_billing_date": (
            tenant.next_billing_date.isoformat() if tenant.next_billing_date else None
        ),
    }


# ── User management ───────────────────────────────────────────────────────────

@router.get("/api/v1/users")
async def list_users(payload: dict = Depends(require_role("admin"))):
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        users = (
            await db.execute(
                select(User)
                .where(User.tenant_id == tenant_id)
                .order_by(User.created_at.asc())
            )
        ).scalars().all()
    return [
        {
            "user_id":       str(u.user_id),
            "email":         u.email,
            "display_name":  u.display_name or u.email,
            "role":          u.role,
            "status":        u.status,
            "last_active_at": u.last_active_at.isoformat() if u.last_active_at else None,
            "invited_at":    u.invited_at.isoformat() if u.invited_at else None,
            "accepted_at":   u.accepted_at.isoformat() if u.accepted_at else None,
        }
        for u in users
    ]


@router.post("/api/v1/users/invite", status_code=201)
async def invite_user(
    body: InviteRequest,
    request: Request,
    payload: dict = Depends(require_role("admin")),
):
    """
    Sends an invite to a new user. In production wire this to an email
    provider (SendGrid, SES). For now the invite_token is returned in the
    response so you can test without email.
    """
    _validate_role(body.role)
    tenant_id   = payload["tenant_id"]
    inviter_id  = payload["sub"]
    email       = body.email.strip().lower()

    async with AsyncSessionLocal() as db:
        # Check tenant user limit
        tenant = (
            await db.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
        ).scalar_one_or_none()
        user_count = (
            await db.execute(
                select(func.count(User.user_id)).where(
                    User.tenant_id == tenant_id,
                    User.status    != UserStatus.DEACTIVATED,
                )
            )
        ).scalar_one()
        if tenant and user_count >= tenant.max_users:
            raise HTTPException(
                400,
                f"User limit reached ({user_count}/{tenant.max_users}). Upgrade your plan.",
            )

        # Check not already invited/active
        existing = (
            await db.execute(
                select(User).where(User.email == email, User.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(400, f"User {email} already exists in this tenant")

        import uuid
        token     = secrets.token_urlsafe(32)
        user_id   = str(uuid.uuid4())
        new_user  = User(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            display_name=body.display_name,
            role=body.role,
            status=UserStatus.PENDING,
            invite_token=token,
            invite_expires_at=datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS),
            invited_by=inviter_id,
            invited_at=datetime.utcnow(),
        )
        db.add(new_user)
        await db.commit()

    # TODO: send email with link: /invite/accept?token={token}
    logger.info("User invited: email=%s role=%s tenant=%s", email, body.role, tenant_id)
    return {
        "user_id":        user_id,
        "email":          email,
        "role":           body.role,
        "status":         "pending",
        "invite_token":   token,          # expose for testing; remove in prod
        "expires_in_hours": INVITE_EXPIRY_HOURS,
        "accept_url":     f"/invite/accept?token={token}",
    }


@router.delete("/api/v1/users/{user_id}")
async def deactivate_user(
    user_id: str,
    payload: dict = Depends(require_role("admin")),
):
    tenant_id = payload["tenant_id"]
    if user_id == payload["sub"]:
        raise HTTPException(400, "You cannot deactivate your own account")

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(
                select(User).where(
                    User.user_id  == user_id,
                    User.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not user:
            raise HTTPException(404, "User not found")
        user.status = UserStatus.DEACTIVATED
        await db.commit()
    return {"deactivated": True, "user_id": user_id}


# ── RFP assignments ───────────────────────────────────────────────────────────

@router.get("/api/v1/rfp/{rfp_id}/assignments")
async def list_assignments(
    rfp_id: str,
    payload: dict = Depends(require_role("admin")),
):
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(RFPAssignment, User)
                .join(User, User.user_id == RFPAssignment.user_id)
                .where(
                    RFPAssignment.rfp_id    == rfp_id,
                    RFPAssignment.tenant_id == tenant_id,
                )
            )
        ).all()
    return [
        {
            "assignment_id":  str(a.assignment_id),
            "user_id":        str(a.user_id),
            "email":          u.email,
            "display_name":   u.display_name or u.email,
            "evaluator_role": a.evaluator_role,
            "is_submitted":   a.is_submitted,
            "submitted_at":   a.submitted_at.isoformat() if a.submitted_at else None,
            "assigned_at":    a.assigned_at.isoformat() if a.assigned_at else None,
        }
        for a, u in rows
    ]


@router.post("/api/v1/rfp/{rfp_id}/assignments", status_code=201)
async def assign_evaluator(
    rfp_id: str,
    body: AssignRequest,
    payload: dict = Depends(require_role("admin")),
):
    _validate_role(body.evaluator_role)
    tenant_id  = payload["tenant_id"]
    assigner   = payload["sub"]

    async with AsyncSessionLocal() as db:
        # Confirm RFP belongs to tenant
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

        # Confirm user belongs to tenant
        user = (
            await db.execute(
                select(User).where(
                    User.user_id   == body.user_id,
                    User.tenant_id == tenant_id,
                    User.status    == UserStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if not user:
            raise HTTPException(404, "Active user not found in this tenant")

        # No duplicate assignment for same role
        existing = (
            await db.execute(
                select(RFPAssignment).where(
                    RFPAssignment.rfp_id        == rfp_id,
                    RFPAssignment.user_id       == body.user_id,
                    RFPAssignment.evaluator_role == body.evaluator_role,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(400, "User already assigned to this RFP with this role")

        import uuid
        assignment = RFPAssignment(
            assignment_id=str(uuid.uuid4()),
            rfp_id=rfp_id,
            user_id=body.user_id,
            tenant_id=tenant_id,
            evaluator_role=body.evaluator_role,
            assigned_by=assigner,
        )
        db.add(assignment)
        await db.commit()

    return {
        "assignment_id":  str(assignment.assignment_id),
        "rfp_id":         rfp_id,
        "user_id":        body.user_id,
        "evaluator_role": body.evaluator_role,
    }


@router.delete("/api/v1/rfp/{rfp_id}/assignments/{assignment_id}")
async def remove_assignment(
    rfp_id: str,
    assignment_id: str,
    payload: dict = Depends(require_role("admin")),
):
    tenant_id = payload["tenant_id"]
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(RFPAssignment).where(
                    RFPAssignment.assignment_id == assignment_id,
                    RFPAssignment.rfp_id        == rfp_id,
                    RFPAssignment.tenant_id     == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(404, "Assignment not found")
        if row.is_submitted:
            raise HTTPException(400, "Cannot remove an evaluator who has already submitted")
        await db.delete(row)
        await db.commit()
    return {"deleted": True, "assignment_id": assignment_id}


# ── RFP state machine transition ──────────────────────────────────────────────

@router.post("/api/v1/rfp/{rfp_id}/transition")
async def transition_rfp(
    rfp_id: str,
    body: TransitionRequest,
    payload: dict = Depends(require_role("admin")),
):
    """
    Advance the RFP state machine. Server validates allowed transitions.
    Some transitions have preconditions (checked below).
    """
    tenant_id = payload["tenant_id"]
    to_state  = body.to.strip()

    async with AsyncSessionLocal() as db:
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

        current = rfp.status
        allowed = VALID_TRANSITIONS.get(current, [])
        if to_state not in [s.value if hasattr(s, "value") else s for s in allowed]:
            raise HTTPException(
                400,
                f"Cannot transition from '{current}' to '{to_state}'. "
                f"Allowed: {[s.value if hasattr(s, 'value') else s for s in allowed]}",
            )

        # Precondition checks
        if to_state == "criteria_approved":
            if not rfp.rubric_locked_at:
                raise HTTPException(400, "Lock the rubric before approving criteria")

        if to_state == "vendors_uploaded":
            vendor_count = (
                await db.execute(
                    select(func.count(
                        __import__("app.models.db", fromlist=["VendorResponse"])
                        .VendorResponse.response_id
                    )).where(
                        __import__("app.models.db", fromlist=["VendorResponse"])
                        .VendorResponse.rfp_id == rfp_id
                    )
                )
            ).scalar_one()
            if vendor_count == 0:
                raise HTTPException(400, "Upload at least one vendor before this transition")

        if to_state == "consensus":
            # All assigned evaluators must have submitted
            assignments = (
                await db.execute(
                    select(RFPAssignment).where(RFPAssignment.rfp_id == rfp_id)
                )
            ).scalars().all()
            unsubmitted = [a for a in assignments if not a.is_submitted]
            if unsubmitted:
                raise HTTPException(
                    400,
                    f"{len(unsubmitted)} evaluator(s) have not yet submitted their scores",
                )

        if to_state == "locked":
            if not rfp.winner_vendor_id:
                raise HTTPException(400, "Declare a winner before locking the RFP")

        # Apply transition
        old_status  = rfp.status
        rfp.status  = to_state
        if to_state == "locked":
            rfp.locked_at = datetime.utcnow()
            rfp.locked_by = payload["sub"]

        await db.commit()

    logger.info("RFP %s transitioned: %s → %s by %s", rfp_id, old_status, to_state, payload.get("sub"))
    return {
        "rfp_id":     rfp_id,
        "old_status": old_status,
        "new_status": to_state,
    }
