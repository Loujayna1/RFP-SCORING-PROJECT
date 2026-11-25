"""
tests/test_phase3_phase4.py
────────────────────────────────────────────────────────────────────────────
Integration tests for:
  - RFP state machine transitions (Step 3)
  - Per-RFP role assignment + criteria visibility (Step 4)
  - Evaluator submit + consensus + winner declaration + lock (Step 5)

Run with:
    pytest tests/test_phase3_phase4.py -v

Fixtures create a complete tenant, users, RFP, vendors, and assignments
so each test has a realistic starting state.
"""

import uuid
import pytest
from datetime import datetime, timedelta
from httpx import AsyncClient
from sqlalchemy import text

from app.main import app
from app.db.session import AsyncSessionLocal
from app.models.db import (
    AuditLog, BusinessVote, EvaluationCriterion, RFPAssignment,
    RFPPackage, RFPStatus, RequirementScore, Requirement,
    Tenant, User, Vendor, VendorResponse, VendorScore,
    UserRole, UserStatus,
)
from app.core.auth import create_token
from tests.conftest import purge_tenant
from passlib.context import CryptContext

_pwd  = CryptContext(schemes=["bcrypt"], deprecated="auto")
BASE  = "http://test"


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tok(user_id: str, tenant_id: str, role: str) -> str:
    from jose import jwt as _jwt
    from app.core.config import settings
    payload = {
        "sub": user_id, "email": f"{role}@test.com",
        "role": role, "tenant_id": tenant_id,
        "exp": int((datetime.utcnow() + timedelta(hours=8)).timestamp()),
    }
    return _jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


async def _make_tenant_and_users(db) -> dict:
    """Create a tenant with admin + one of each evaluator role."""
    tid = str(uuid.uuid4())
    db.add(Tenant(
        tenant_id=tid, name="Test Corp",
        slug=f"test-corp-{tid[:6]}", plan="pro", status="active",
    ))
    await db.flush()

    users = {}
    for role in [
        UserRole.ADMIN,
        UserRole.TECHNICAL_EVALUATOR,
        UserRole.FINANCIAL_EVALUATOR,
        UserRole.BUSINESS_REP,
    ]:
        uid = str(uuid.uuid4())
        db.add(User(
            user_id=uid, tenant_id=tid, email=f"{role.value}-{tid[:6]}@test.com",
            hashed_password=_pwd.hash("password"),
            role=role.value, status=UserStatus.ACTIVE,
            accepted_at=datetime.utcnow(),
        ))
        users[role.value] = uid
    await db.flush()
    return {"tenant_id": tid, "users": users}


async def _make_rfp(db, tenant_id: str, status: str = "draft") -> str:
    rid = str(uuid.uuid4())
    db.add(RFPPackage(
        rfp_id=rid, tenant_id=tenant_id,
        title="Cloud Migration 2026",
        status=status,
        rubric_locked_at=datetime.utcnow() if status != "draft" else None,
    ))
    await db.flush()
    return rid


async def _make_criteria(db, rfp_id: str) -> list[str]:
    cids = []
    for label, owner_role, weight in [
        ("Technical Architecture",   "technical_evaluator",  0.40),
        ("Cost & Pricing",           "financial_evaluator",  0.35),
        ("Implementation Timeline",  None,                   0.25),
    ]:
        cid = str(uuid.uuid4())
        db.add(EvaluationCriterion(
            criterion_id=cid, rfp_id=rfp_id,
            label=label, section=label, weight=weight,
            max_score=5, criterion_type="scored",
            owner_role=owner_role,
        ))
        cids.append(cid)
    await db.flush()
    return cids


async def _make_vendor_and_scores(db, rfp_id: str, tenant_id: str, criterion_ids: list[str]) -> tuple[str, str]:
    vid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    db.add(Vendor(vendor_id=vid, tenant_id=tenant_id, name="Acme Cloud"))
    db.add(VendorResponse(
        response_id=rid, rfp_id=rfp_id, vendor_id=vid,
        tenant_id=tenant_id, status="ready",
    ))
    await db.flush()

    # Fake requirement
    req_id = str(uuid.uuid4())
    db.add(Requirement(
        req_id=req_id, rfp_id=rfp_id,
        req_code="REQ-001", description="Must support 99.9% SLA",
        category="Technical", is_mandatory=True,
    ))
    await db.flush()

    for cid in criterion_ids:
        db.add(RequirementScore(
            score_id=str(uuid.uuid4()),
            req_id=req_id, response_id=rid,
            criterion_id=cid,
            raw_score=3.5, ai_score=3.5,
            compliance_level="PartiallyCompliant",
            justification="AI-generated score",
            evaluator_role="ai",
        ))
    db.add(VendorScore(
        vendor_score_id=str(uuid.uuid4()),
        rfp_id=rfp_id, response_id=rid,
        total_score=3.5, section_scores={},
    ))
    await db.flush()
    return vid, rid


async def _assign(db, rfp_id: str, user_id: str, tenant_id: str, role: str) -> str:
    aid = str(uuid.uuid4())
    db.add(RFPAssignment(
        assignment_id=aid, rfp_id=rfp_id,
        user_id=user_id, tenant_id=tenant_id,
        evaluator_role=role, is_submitted=False,
    ))
    await db.flush()
    return aid


@pytest.fixture
async def ctx():
    """Full test context: tenant, users, RFP, criteria, vendor, assignments."""
    async with AsyncSessionLocal() as db:
        info      = await _make_tenant_and_users(db)
        tid       = info["tenant_id"]
        users     = info["users"]
        rfp_id    = await _make_rfp(db, tid, status="ai_eval_complete")
        crit_ids  = await _make_criteria(db, rfp_id)
        vid, rid  = await _make_vendor_and_scores(db, rfp_id, tid, crit_ids)
        tech_aid  = await _assign(db, rfp_id, users["technical_evaluator"], tid, "technical_evaluator")
        fin_aid   = await _assign(db, rfp_id, users["financial_evaluator"],  tid, "financial_evaluator")
        await db.commit()

    tokens = {role: _tok(uid, tid, role) for role, uid in users.items()}

    yield {
        "tenant_id":   tid,
        "users":       users,
        "tokens":      tokens,
        "rfp_id":      rfp_id,
        "crit_ids":    crit_ids,
        "vendor_id":   vid,
        "response_id": rid,
        "tech_aid":    tech_aid,
        "fin_aid":     fin_aid,
    }

    # Cleanup
    await purge_tenant(tid)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — State machine
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_valid(ctx):
    """Admin can transition from ai_eval_complete → human_eval_in_progress."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"to": "human_eval_in_progress"},
        )
    assert r.status_code == 200
    assert r.json()["new_status"] == "human_eval_in_progress"


@pytest.mark.asyncio
async def test_transition_invalid_jump(ctx):
    """Cannot skip from ai_eval_complete directly to locked."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"to": "locked"},
        )
    assert r.status_code == 400
    assert "Cannot transition" in r.json()["detail"]


@pytest.mark.asyncio
async def test_transition_requires_admin(ctx):
    """Technical evaluator cannot call transition."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['technical_evaluator']}"},
            json={"to": "human_eval_in_progress"},
        )
    assert r.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Criteria visibility + assignments
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluator_dashboard_filters_by_role(ctx):
    """Technical evaluator only sees technical criteria, not financial."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/dashboard",
            headers={"Authorization": f"Bearer {ctx['tokens']['technical_evaluator']}"},
        )
    assert r.status_code == 200
    data = r.json()
    labels = [
        c["criterion_label"]
        for vendor in data["vendors"]
        for c in vendor["scores"]
    ]
    assert all("Cost" not in label for label in labels), \
        "Financial criteria must not appear in technical evaluator's dashboard"
    assert any("Technical" in label for label in labels), \
        "Technical criteria must appear"


@pytest.mark.asyncio
async def test_evaluator_dashboard_unassigned_user_denied(ctx):
    """Business rep (not assigned as evaluator) gets 403."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/dashboard",
            headers={"Authorization": f"Bearer {ctx['tokens']['business_rep']}"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_assign_evaluator_creates_assignment(ctx):
    """Admin can assign an evaluator to an RFP."""
    new_uid = ctx["users"]["business_rep"]
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/assignments",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"user_id": new_uid, "evaluator_role": "business_rep"},
        )
    assert r.status_code == 201
    assert r.json()["evaluator_role"] == "business_rep"


@pytest.mark.asyncio
async def test_cannot_assign_duplicate_role(ctx):
    """Cannot assign the same user+role twice."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/assignments",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={
                "user_id":        ctx["users"]["technical_evaluator"],
                "evaluator_role": "technical_evaluator",
            },
        )
    assert r.status_code == 400
    assert "already assigned" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Submit, consensus, winner, lock
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluator_submit_marks_assignment(ctx):
    """Technical evaluator submit → assignment.is_submitted = True."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/submit",
            headers={"Authorization": f"Bearer {ctx['tokens']['technical_evaluator']}"},
            json={"confirm": True},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "submitted"
    assert data["submitted_count"] == 1
    assert data["evaluator_count"] == 2  # tech + financial assigned


@pytest.mark.asyncio
async def test_submit_twice_is_rejected(ctx):
    """Submitting twice returns 409."""
    tok = ctx["tokens"]["technical_evaluator"]
    rfp = ctx["rfp_id"]
    async with AsyncClient(app=app, base_url=BASE) as client:
        await client.post(
            f"/api/v1/rfp/{rfp}/evaluator/submit",
            headers={"Authorization": f"Bearer {tok}"},
            json={"confirm": True},
        )
        r = await client.post(
            f"/api/v1/rfp/{rfp}/evaluator/submit",
            headers={"Authorization": f"Bearer {tok}"},
            json={"confirm": True},
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_all_submit_auto_transitions_to_consensus(ctx):
    """When all evaluators submit the RFP auto-moves to consensus."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        # Transition to human_eval_in_progress first
        await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"to": "human_eval_in_progress"},
        )
        # Both evaluators submit
        for role in ["technical_evaluator", "financial_evaluator"]:
            await client.post(
                f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/submit",
                headers={"Authorization": f"Bearer {ctx['tokens'][role]}"},
                json={"confirm": True},
            )

        # Check RFP is now in consensus
        r = await client.get(
            f"/api/v1/rfp/{ctx['rfp_id']}",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
        )
    assert r.json()["status"] == "consensus"


@pytest.mark.asyncio
async def test_consensus_view_available_after_all_submit(ctx):
    """GET /consensus returns all scores after auto-transition."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"to": "human_eval_in_progress"},
        )
        for role in ["technical_evaluator", "financial_evaluator"]:
            await client.post(
                f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/submit",
                headers={"Authorization": f"Bearer {ctx['tokens'][role]}"},
                json={"confirm": True},
            )
        r = await client.get(
            f"/api/v1/rfp/{ctx['rfp_id']}/consensus",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
        )
    assert r.status_code == 200
    data = r.json()
    assert "vendors" in data
    assert len(data["vendors"]) >= 1
    assert data["rfp_status"] == "consensus"


@pytest.mark.asyncio
async def test_consensus_view_blocked_before_consensus_state(ctx):
    """GET /consensus returns 400 when RFP is not in consensus state."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            f"/api/v1/rfp/{ctx['rfp_id']}/consensus",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_business_vote_cast_and_update(ctx):
    """Business rep can cast and then update a vote."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        # Assign business_rep first
        await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/assignments",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"user_id": ctx["users"]["business_rep"], "evaluator_role": "business_rep"},
        )
        r1 = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/vendor/{ctx['response_id']}/vote",
            headers={"Authorization": f"Bearer {ctx['tokens']['business_rep']}"},
            json={"vote": "yes", "comment": "Looks good"},
        )
        assert r1.status_code == 200
        assert r1.json()["vote"] == "yes"

        # Update vote
        r2 = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/vendor/{ctx['response_id']}/vote",
            headers={"Authorization": f"Bearer {ctx['tokens']['business_rep']}"},
            json={"vote": "maybe", "comment": "Need more info"},
        )
        assert r2.status_code == 200
        assert r2.json()["vote"] == "maybe"


@pytest.mark.asyncio
async def test_declare_winner(ctx):
    """Admin can declare a winner in consensus state."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        # Get to consensus state
        await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"to": "human_eval_in_progress"},
        )
        for role in ["technical_evaluator", "financial_evaluator"]:
            await client.post(
                f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/submit",
                headers={"Authorization": f"Bearer {ctx['tokens'][role]}"},
                json={"confirm": True},
            )
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/declare-winner",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={
                "vendor_id": ctx["vendor_id"],
                "reason":    "Highest technical score with best value pricing",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "winner_declared"
    assert data["winner_vendor_id"] == ctx["vendor_id"]


@pytest.mark.asyncio
async def test_lock_without_winner_fails(ctx):
    """Cannot lock before declaring a winner."""
    async with AsyncClient(app=app, base_url=BASE) as client:
        await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
            json={"to": "human_eval_in_progress"},
        )
        for role in ["technical_evaluator", "financial_evaluator"]:
            await client.post(
                f"/api/v1/rfp/{ctx['rfp_id']}/evaluator/submit",
                headers={"Authorization": f"Bearer {ctx['tokens'][role]}"},
                json={"confirm": True},
            )
        r = await client.post(
            f"/api/v1/rfp/{ctx['rfp_id']}/lock",
            headers={"Authorization": f"Bearer {ctx['tokens']['admin']}"},
        )
    assert r.status_code == 400
    assert "winner" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_full_phase3_to_lock_flow(ctx):
    """End-to-end: ai_eval_complete → submit → consensus → declare → lock."""
    rfp_id = ctx["rfp_id"]
    admin  = ctx["tokens"]["admin"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        # 1. Transition to human_eval_in_progress
        r = await client.post(
            f"/api/v1/rfp/{rfp_id}/transition",
            headers={"Authorization": f"Bearer {admin}"},
            json={"to": "human_eval_in_progress"},
        )
        assert r.json()["new_status"] == "human_eval_in_progress"

        # 2. Both evaluators submit (auto-transitions to consensus)
        for role in ["technical_evaluator", "financial_evaluator"]:
            await client.post(
                f"/api/v1/rfp/{rfp_id}/evaluator/submit",
                headers={"Authorization": f"Bearer {ctx['tokens'][role]}"},
                json={"confirm": True},
            )

        # 3. Save consensus notes
        await client.post(
            f"/api/v1/rfp/{rfp_id}/consensus/notes",
            headers={"Authorization": f"Bearer {admin}"},
            json={"notes": "Team agreed Acme Cloud best overall value"},
        )

        # 4. Declare winner
        await client.post(
            f"/api/v1/rfp/{rfp_id}/declare-winner",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "vendor_id": ctx["vendor_id"],
                "reason":    "Best technical architecture and competitive pricing",
            },
        )

        # 5. Lock RFP
        r = await client.post(
            f"/api/v1/rfp/{rfp_id}/lock",
            headers={"Authorization": f"Bearer {admin}"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "locked"
    assert data["winner_name"] == "Acme Cloud"

    # Verify in DB
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                text(f"SELECT status, locked_at FROM rfp_package WHERE rfp_id = '{rfp_id}'")
            )
        ).fetchone()
    assert rfp[0] == "locked"
    assert rfp[1] is not None


@pytest.mark.asyncio
async def test_locked_rfp_rejects_changes(ctx):
    """After locking, score overrides are rejected."""
    rfp_id = ctx["rfp_id"]
    admin  = ctx["tokens"]["admin"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        # Fast-track to locked via DB (avoid full flow)
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    f"UPDATE rfp_package SET status='consensus', "
                    f"winner_vendor_id='{ctx['vendor_id']}' "
                    f"WHERE rfp_id='{rfp_id}'"
                )
            )
            # Mark all assignments submitted
            await db.execute(
                text(
                    f"UPDATE rfp_assignment SET is_submitted=true "
                    f"WHERE rfp_id='{rfp_id}'"
                )
            )
            await db.commit()

        await client.post(
            f"/api/v1/rfp/{rfp_id}/declare-winner",
            headers={"Authorization": f"Bearer {admin}"},
            json={"vendor_id": ctx["vendor_id"], "reason": "Best overall"},
        )
        await client.post(
            f"/api/v1/rfp/{rfp_id}/lock",
            headers={"Authorization": f"Bearer {admin}"},
        )

        # Try consensus override after lock
        r = await client.patch(
            f"/api/v1/rfp/{rfp_id}/consensus/override",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "score_id":        str(uuid.uuid4()),
                "final_score":     4.0,
                "override_reason": "Testing locked state",
            },
        )
    # Should be rejected — locked RFP
    assert r.status_code in (400, 404)


@pytest.mark.asyncio
async def test_audit_log_entries_created_for_all_actions(ctx):
    """Key actions produce audit log entries."""
    rfp_id = ctx["rfp_id"]
    admin  = ctx["tokens"]["admin"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        await client.post(
            f"/api/v1/rfp/{rfp_id}/transition",
            headers={"Authorization": f"Bearer {admin}"},
            json={"to": "human_eval_in_progress"},
        )
        await client.post(
            f"/api/v1/rfp/{rfp_id}/evaluator/submit",
            headers={"Authorization": f"Bearer {ctx['tokens']['technical_evaluator']}"},
            json={"confirm": True},
        )

    async with AsyncSessionLocal() as db:
        entries = (
            await db.execute(
                text(
                    f"SELECT action FROM audit_log WHERE rfp_id='{rfp_id}' "
                    f"ORDER BY created_at"
                )
            )
        ).fetchall()

    actions = [e[0] for e in entries]
    assert "evaluator_submitted" in actions, "Submit must produce audit entry"
