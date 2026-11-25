"""
Tests for human review endpoints.

Run with:
    pytest backend/tests/test_human_review.py -v

Requires a live test DB. Set TEST_DATABASE_URL in .env.test
or the tests will use the value in settings.DATABASE_URL.

Fixtures create a minimal RFP + criteria + vendor response + scores
then tear everything down after each test.
"""

import uuid
from types import SimpleNamespace
import pytest
import pytest_asyncio
from datetime import datetime
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.main import app
from app.core.auth import create_token
from tests.conftest import purge_tenant
from app.core.config import settings
from app.models import db as models_db

# Use NullPool so connections are not cached across event loops.
_test_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)
TestSessionLocal = async_sessionmaker(
    _test_engine, class_=AsyncSession,
    expire_on_commit=False, autoflush=False, autocommit=False,
)


# ═════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def client(seed):
    """Client authenticated as an admin of the seeded tenant."""
    token = create_token(SimpleNamespace(
        user_id=uuid.uuid4(), email="officer@company.com",
        role="admin", tenant_id=seed["tenant_id"],
    ))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c


@pytest_asyncio.fixture
async def seed():
    """
    Creates:
      - 1 RFP package
      - 3 EvaluationCriterion rows (2 scored, 1 mandatory)
      - 1 Vendor + VendorResponse
      - 3 Requirement rows
      - 3 RequirementScore rows (one flagged)
      - 1 VendorScore row

    Returns a dict of all IDs.
    Cleans up after the test.
    """
    tenant_id   = str(uuid.uuid4())
    rfp_id      = str(uuid.uuid4())
    vendor_id   = str(uuid.uuid4())
    response_id = str(uuid.uuid4())
    c1_id = str(uuid.uuid4())
    c2_id = str(uuid.uuid4())
    c3_id = str(uuid.uuid4())
    r1_id = str(uuid.uuid4())
    r2_id = str(uuid.uuid4())
    r3_id = str(uuid.uuid4())
    s1_id = str(uuid.uuid4())
    s2_id = str(uuid.uuid4())
    s3_id = str(uuid.uuid4())

    async with TestSessionLocal() as db:
        # ── Phase 1: parent rows ──────────────────────────────────────────
        db.add(models_db.Tenant(
            tenant_id=tenant_id,
            name="Test Tenant",
            slug=f"test-tenant-{tenant_id[:8]}",
        ))
        db.add(models_db.RFPPackage(
            rfp_id=rfp_id, tenant_id=tenant_id, title="Test RFP", status="scored",
            created_at=datetime.utcnow(),
        ))
        db.add(models_db.Vendor(
            vendor_id=vendor_id, tenant_id=tenant_id, name="Acme Corp", created_at=datetime.utcnow(),
        ))
        db.add(models_db.VendorResponse(
            response_id=response_id, rfp_id=rfp_id, vendor_id=vendor_id,
            tenant_id=tenant_id, status="scored", created_at=datetime.utcnow(),
        ))

        # Criteria
        db.add(models_db.EvaluationCriterion(
            criterion_id=c1_id, rfp_id=rfp_id,
            label="Technical Approach", section="Technical",
            weight=0.5, max_score=5,
            criterion_type="scored",
            confidence_score=0.90,
            extraction_source="structured_table",
        ))
        db.add(models_db.EvaluationCriterion(
            criterion_id=c2_id, rfp_id=rfp_id,
            label="Past Performance", section="Experience",
            weight=0.5, max_score=5,
            criterion_type="scored",
            confidence_score=0.60,        # LOW — should be flagged
            extraction_source="llm_prose",
        ))
        db.add(models_db.EvaluationCriterion(
            criterion_id=c3_id, rfp_id=rfp_id,
            label="Data Security", section="Compliance",
            weight=0.0, max_score=5,
            criterion_type="mandatory",
            confidence_score=0.95,
            extraction_source="structured_table",
        ))

        # Requirements — flush to DB before adding scores that reference them
        for rid, code, cat in [
            (r1_id, "REQ-01", "Technical"),
            (r2_id, "REQ-02", "Experience"),
            (r3_id, "REQ-03", "Compliance"),
        ]:
            req_kwargs = {
                "req_id": rid,
                "rfp_id": rfp_id,
                "req_code": code,
                "description": f"Requirement {code}",
                "category": cat,
                "is_mandatory": (cat == "Compliance"),
            }
            if hasattr(models_db.Requirement, "created_at"):
                req_kwargs["created_at"] = datetime.utcnow()
            db.add(models_db.Requirement(**req_kwargs))

        # Flush parents + requirements so FK references exist
        await db.flush()

        # ── Phase 2: child rows (RequirementScore, VendorScore) ───────────
        db.add(models_db.RequirementScore(
            score_id=s1_id, req_id=r1_id, response_id=response_id,
            criterion_id=c1_id, raw_score=4.0, ai_score=4.0,
            compliance_level="compliant",
            justification="Strong methodology described.",
            confidence_flag=False, scored_at=datetime.utcnow(),
        ))
        db.add(models_db.RequirementScore(
            score_id=s2_id, req_id=r2_id, response_id=response_id,
            criterion_id=c2_id, raw_score=2.5, ai_score=2.5,
            compliance_level="partial",
            justification="Limited evidence of past contracts.",
            confidence_flag=True,   # flagged — low similarity
            scored_at=datetime.utcnow(),
        ))
        db.add(models_db.RequirementScore(
            score_id=s3_id, req_id=r3_id, response_id=response_id,
            criterion_id=c3_id, raw_score=5.0, ai_score=5.0,
            compliance_level="compliant",
            justification="ISO 27001 certificate present.",
            confidence_flag=False, scored_at=datetime.utcnow(),
        ))

        db.add(models_db.VendorScore(
            rfp_id=rfp_id, response_id=response_id,
            total_score=72.5,
            section_scores={"Technical Approach": 4.0, "Past Performance": 2.5, "Data Security": 5.0},
            computed_at=datetime.utcnow(),
        ))

        await db.commit()

    yield {
        "tenant_id": tenant_id,
        "rfp_id": rfp_id, "vendor_id": vendor_id, "response_id": response_id,
        "c1_id": c1_id, "c2_id": c2_id, "c3_id": c3_id,
        "r1_id": r1_id, "r2_id": r2_id, "r3_id": r3_id,
        "s1_id": s1_id, "s2_id": s2_id, "s3_id": s3_id,
    }

    await purge_tenant(tenant_id)


# ═════════════════════════════════════════════════════════════════════════════
# RUBRIC TESTS
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_rubric_returns_all_criteria(client, seed):
    r = await client.get(f"/api/v1/rfp/{seed['rfp_id']}/rubric")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert data["rubric_locked"] is False
    # Low-confidence criterion (c2) should be flagged
    flagged = [c for c in data["criteria"] if c["needs_review"]]
    assert len(flagged) == 1
    assert flagged[0]["criterion_id"] == seed["c2_id"]


@pytest.mark.asyncio
async def test_rubric_status_unlocked(client, seed):
    r = await client.get(f"/api/v1/rfp/{seed['rfp_id']}/rubric/status")
    assert r.status_code == 200
    assert r.json()["rubric_locked"] is False


@pytest.mark.asyncio
async def test_patch_rubric_updates_weight(client, seed):
    payload = {
        "actor": "officer@company.com",
        "criteria": [
            {"criterion_id": seed["c1_id"], "weight": 0.6},
            {"criterion_id": seed["c2_id"], "weight": 0.4},
        ],
    }
    r = await client.patch(f"/api/v1/rfp/{seed['rfp_id']}/rubric", json=payload)
    assert r.status_code == 200
    updated = {c["criterion_id"]: c for c in r.json()["criteria"]}
    # Weights should re-normalise to sum 1.0 across scored criteria
    scored = [c for c in r.json()["criteria"] if c["criterion_type"] != "mandatory"]
    total_weight = sum(c["weight"] for c in scored)
    assert abs(total_weight - 1.0) < 0.01


@pytest.mark.asyncio
async def test_patch_rubric_invalid_type_rejected(client, seed):
    payload = {
        "actor": "officer@company.com",
        "criteria": [{"criterion_id": seed["c1_id"], "criterion_type": "invalid_type"}],
    }
    r = await client.patch(f"/api/v1/rfp/{seed['rfp_id']}/rubric", json=payload)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_lock_rubric(client, seed):
    r = await client.post(
        f"/api/v1/rfp/{seed['rfp_id']}/rubric/lock",
        json={"actor": "officer@company.com"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "locked"
    assert data["approved_by"] == "officer@company.com"
    assert data["criteria_count"] == 3


@pytest.mark.asyncio
async def test_lock_rubric_idempotent(client, seed):
    """Locking twice returns already_locked, not an error."""
    body = {"actor": "officer@company.com"}
    await client.post(f"/api/v1/rfp/{seed['rfp_id']}/rubric/lock", json=body)
    r = await client.post(f"/api/v1/rfp/{seed['rfp_id']}/rubric/lock", json=body)
    assert r.status_code == 200
    assert r.json()["status"] == "already_locked"


@pytest.mark.asyncio
async def test_patch_after_lock_rejected(client, seed):
    await client.post(
        f"/api/v1/rfp/{seed['rfp_id']}/rubric/lock",
        json={"actor": "officer@company.com"},
    )
    payload = {
        "actor": "officer@company.com",
        "criteria": [{"criterion_id": seed["c1_id"], "weight": 0.9}],
    }
    r = await client.patch(f"/api/v1/rfp/{seed['rfp_id']}/rubric", json=payload)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_get_rubric_unknown_rfp(client):
    r = await client.get(f"/api/v1/rfp/{uuid.uuid4()}/rubric")
    assert r.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# SCORE REVIEW TESTS
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_flagged_scores_returns_only_flagged(client, seed):
    r = await client.get(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1               # only s2 is flagged
    assert data["scores"][0]["confidence_flag"] is True
    assert data["scores"][0]["ai_score"] == 2.5
    assert data["scores"][0]["human_score"] is None


@pytest.mark.asyncio
async def test_get_all_scores(client, seed):
    r = await client.get(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review?all_scores=true"
    )
    assert r.status_code == 200
    assert r.json()["total"] == 3


@pytest.mark.asyncio
async def test_override_score_success(client, seed):
    payload = {
        "human_score":     4.0,
        "override_reason": "Evidence found on page 12 is strong.",
        "actor":           "officer@company.com",
    }
    r = await client.patch(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review/{seed['s2_id']}",
        json=payload,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "overridden"
    assert data["ai_score"] == 2.5          # original preserved
    assert data["human_score"] == 4.0
    assert data["override_reason"] == "Evidence found on page 12 is strong."
    assert "new_total_score" in data


@pytest.mark.asyncio
async def test_override_clears_confidence_flag(client, seed):
    payload = {
        "human_score": 3.0,
        "override_reason": "Reviewed manually and confirmed.",
        "actor": "officer@company.com",
    }
    await client.patch(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review/{seed['s2_id']}",
        json=payload,
    )
    # After override the flag should be cleared
    r = await client.get(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review"
    )
    assert r.json()["total"] == 0           # no more flagged scores


@pytest.mark.asyncio
async def test_override_score_out_of_range(client, seed):
    payload = {
        "human_score":     99.0,            # > max_score of 5
        "override_reason": "Testing range validation.",
        "actor":           "officer@company.com",
    }
    r = await client.patch(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review/{seed['s2_id']}",
        json=payload,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_override_mandatory_criterion_mid_value_rejected(client, seed):
    """Mandatory criteria only accept 0 or max_score."""
    payload = {
        "human_score":     3.0,             # not 0 or 5
        "override_reason": "Testing mandatory criterion rule.",
        "actor":           "officer@company.com",
    }
    r = await client.patch(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review/{seed['s3_id']}",
        json=payload,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_override_blank_reason_rejected(client, seed):
    payload = {
        "human_score":     3.0,
        "override_reason": "   ",           # blank after strip
        "actor":           "officer@company.com",
    }
    r = await client.patch(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review/{seed['s2_id']}",
        json=payload,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_override_unknown_score_id(client, seed):
    payload = {
        "human_score":     3.0,
        "override_reason": "Legitimate reason.",
        "actor":           "officer@company.com",
    }
    r = await client.patch(
        f"/api/v1/rfp/{seed['rfp_id']}/vendor/{seed['response_id']}/review/{uuid.uuid4()}",
        json=payload,
    )
    assert r.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# CONFLICT DETECTION TESTS
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_conflicts_with_single_vendor_returns_empty(client, seed):
    """Only one vendor in seed — no cross-vendor comparison possible."""
    r = await client.get(f"/api/v1/rfp/{seed['rfp_id']}/conflicts")
    assert r.status_code == 200
    data = r.json()
    assert data["conflict_count"] == 0


@pytest.mark.asyncio
async def test_conflicts_unknown_rfp(client):
    r = await client.get(f"/api/v1/rfp/{uuid.uuid4()}/conflicts")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_conflicts_custom_threshold(client, seed):
    """Custom threshold parameter is accepted."""
    r = await client.get(f"/api/v1/rfp/{seed['rfp_id']}/conflicts?threshold=0.1")
    assert r.status_code == 200
    assert r.json()["threshold"] == 0.1


@pytest.mark.asyncio
async def test_conflicts_uses_criterion_specific_max_score(client, seed):
    """The same raw spread should conflict for max_score=5 but not max_score=10."""
    extra_vendor_id = str(uuid.uuid4())
    extra_response_id = str(uuid.uuid4())
    extra_criterion_id = str(uuid.uuid4())
    low_max_req_id = str(uuid.uuid4())
    high_max_req_id = str(uuid.uuid4())
    low_max_score_id = str(uuid.uuid4())
    low_max_score_id_2 = str(uuid.uuid4())
    high_max_score_id = str(uuid.uuid4())
    high_max_score_id_2 = str(uuid.uuid4())

    async with TestSessionLocal() as db:
        db.add(models_db.Vendor(
            vendor_id=extra_vendor_id,
            tenant_id=seed["tenant_id"],
            name="Beta Corp",
            created_at=datetime.utcnow(),
        ))
        db.add(models_db.VendorResponse(
            response_id=extra_response_id,
            rfp_id=seed["rfp_id"],
            vendor_id=extra_vendor_id,
            tenant_id=seed["tenant_id"],
            status="scored",
            created_at=datetime.utcnow(),
        ))
        db.add(models_db.EvaluationCriterion(
            criterion_id=extra_criterion_id,
            rfp_id=seed["rfp_id"],
            label="Strategic Fit",
            section="Strategy",
            weight=0.0,
            max_score=10,
            criterion_type="scored",
            confidence_score=0.95,
            extraction_source="structured_table",
        ))

        for rid, code, description in [
            (low_max_req_id, "REQ-04", "Requirement with max score 5"),
            (high_max_req_id, "REQ-05", "Requirement with max score 10"),
        ]:
            req_kwargs = {
                "req_id": rid,
                "rfp_id": seed["rfp_id"],
                "req_code": code,
                "description": description,
                "category": "Strategy",
                "is_mandatory": False,
            }
            if hasattr(models_db.Requirement, "created_at"):
                req_kwargs["created_at"] = datetime.utcnow()
            db.add(models_db.Requirement(**req_kwargs))

        await db.flush()

        db.add_all([
            models_db.RequirementScore(
                score_id=low_max_score_id,
                req_id=low_max_req_id,
                response_id=seed["response_id"],
                criterion_id=seed["c1_id"],
                raw_score=4.0,
                ai_score=4.0,
                compliance_level="compliant",
                justification="Primary vendor scored higher.",
                confidence_flag=False,
                scored_at=datetime.utcnow(),
            ),
            models_db.RequirementScore(
                score_id=low_max_score_id_2,
                req_id=low_max_req_id,
                response_id=extra_response_id,
                criterion_id=seed["c1_id"],
                raw_score=3.0,
                ai_score=3.0,
                compliance_level="partial",
                justification="Secondary vendor scored lower.",
                confidence_flag=False,
                scored_at=datetime.utcnow(),
            ),
            models_db.RequirementScore(
                score_id=high_max_score_id,
                req_id=high_max_req_id,
                response_id=seed["response_id"],
                criterion_id=extra_criterion_id,
                raw_score=4.0,
                ai_score=4.0,
                compliance_level="compliant",
                justification="Primary vendor scored higher.",
                confidence_flag=False,
                scored_at=datetime.utcnow(),
            ),
            models_db.RequirementScore(
                score_id=high_max_score_id_2,
                req_id=high_max_req_id,
                response_id=extra_response_id,
                criterion_id=extra_criterion_id,
                raw_score=3.0,
                ai_score=3.0,
                compliance_level="partial",
                justification="Secondary vendor scored lower.",
                confidence_flag=False,
                scored_at=datetime.utcnow(),
            ),
        ])
        await db.commit()

    try:
        r = await client.get(f"/api/v1/rfp/{seed['rfp_id']}/conflicts?threshold=0.15")
        assert r.status_code == 200
        data = r.json()
        assert data["conflict_count"] == 1
        assert data["clean_count"] == 1
        assert len(data["conflicts"]) == 1
        conflict = data["conflicts"][0]
        assert conflict["req_code"] == "REQ-04"
        assert conflict["max_score"] == 5
        assert conflict["spread"] == 1.0
        assert conflict["spread_pct"] == 0.2
    finally:
        from sqlalchemy import delete

        async with TestSessionLocal() as db:
            for model, col, val in [
                (models_db.RequirementScore, models_db.RequirementScore.req_id, low_max_req_id),
                (models_db.RequirementScore, models_db.RequirementScore.req_id, high_max_req_id),
                (models_db.Requirement, models_db.Requirement.req_id, low_max_req_id),
                (models_db.Requirement, models_db.Requirement.req_id, high_max_req_id),
                (models_db.VendorResponse, models_db.VendorResponse.response_id, extra_response_id),
                (models_db.Vendor, models_db.Vendor.vendor_id, extra_vendor_id),
                (models_db.EvaluationCriterion, models_db.EvaluationCriterion.criterion_id, extra_criterion_id),
            ]:
                await db.execute(delete(model).where(col == val))
            await db.commit()
