"""
tests/test_tenant_isolation.py
────────────────────────────────────────────────────────────────────────────
Proves that tenant A cannot read, write, or enumerate tenant B's data
across every endpoint that holds tenant-scoped rows.

Run with:
    pytest tests/test_tenant_isolation.py -v

These tests create two real tenants in the test DB, call real API endpoints,
and assert cross-tenant requests return 403 or 404 (never 200 with the
other tenant's data).
"""

import pytest
import uuid
from datetime import datetime
from httpx import AsyncClient
from sqlalchemy import text

from app.main import app
from app.db.session import AsyncSessionLocal
from app.models.db import (
    Tenant, User, RFPPackage, Vendor, VendorResponse,
    UserRole, UserStatus, RFPStatus,
)
from app.core.auth import create_token
from passlib.context import CryptContext
from tests.conftest import purge_tenant

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

BASE = "http://test"


# ── Fixtures ──────────────────────────────────────────────────────────────────

async def _make_tenant(db, name: str) -> tuple[str, str]:
    """Create a tenant + admin user. Returns (tenant_id, user_id)."""
    tid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    db.add(Tenant(
        tenant_id=tid, name=name,
        slug=name.lower().replace(" ", "-") + "-" + tid[:6],
        plan="pro", status="active",
    ))
    db.add(User(
        user_id=uid, tenant_id=tid,
        email=f"admin-{tid[:6]}@test.com",
        hashed_password=_pwd.hash("password"),
        role=UserRole.ADMIN, status=UserStatus.ACTIVE,
        accepted_at=datetime.utcnow(),
    ))
    await db.flush()
    return tid, uid


async def _make_rfp(db, tenant_id: str, title: str) -> str:
    rid = str(uuid.uuid4())
    db.add(RFPPackage(
        rfp_id=rid, tenant_id=tenant_id,
        title=title, status=RFPStatus.DRAFT,
    ))
    await db.flush()
    return rid


def _token(user_id: str, tenant_id: str, role: str = "admin") -> str:
    """Build a JWT for a synthetic user without DB lookup."""
    from jose import jwt as _jwt
    from app.core.config import settings
    from datetime import timedelta
    payload = {
        "sub": user_id, "email": f"{user_id[:6]}@test.com",
        "role": role, "tenant_id": tenant_id,
        "exp": int((datetime.utcnow() + timedelta(hours=8)).timestamp()),
    }
    return _jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


@pytest.fixture
async def two_tenants():
    """Creates two independent tenants with one RFP each. Yields both contexts."""
    async with AsyncSessionLocal() as db:
        t1_id, u1_id = await _make_tenant(db, "Tenant Alpha")
        t2_id, u2_id = await _make_tenant(db, "Tenant Beta")
        r1_id = await _make_rfp(db, t1_id, "Alpha RFP")
        r2_id = await _make_rfp(db, t2_id, "Beta RFP")
        await db.commit()

    yield {
        "t1": {"tenant_id": t1_id, "user_id": u1_id, "rfp_id": r1_id,
               "token": _token(u1_id, t1_id)},
        "t2": {"tenant_id": t2_id, "user_id": u2_id, "rfp_id": r2_id,
               "token": _token(u2_id, t2_id)},
    }

    # Cleanup — delete test tenants and everything they own
    for tid in [t1_id, t2_id]:
        await purge_tenant(tid)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_cannot_read_other_rfp_list(two_tenants):
    """Tenant A's token must not return Tenant B's RFP in the list."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            "/api/v1/rfp",
            headers={"Authorization": f"Bearer {t1['token']}"},
        )
    assert r.status_code == 200
    rfp_ids = [item["rfp_id"] for item in r.json()]
    assert t2["rfp_id"] not in rfp_ids, "Tenant A must not see Tenant B's RFPs"
    assert t1["rfp_id"] in rfp_ids, "Tenant A must see its own RFPs"


@pytest.mark.asyncio
async def test_tenant_cannot_get_other_rfp_directly(two_tenants):
    """GET /rfp/{id} with Tenant A's token must not return Tenant B's RFP."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            f"/api/v1/rfp/{t2['rfp_id']}",
            headers={"Authorization": f"Bearer {t1['token']}"},
        )
    assert r.status_code == 404, (
        f"Expected 404, got {r.status_code}: {r.text}"
    )


@pytest.mark.asyncio
async def test_tenant_cannot_add_vendor_to_other_rfp(two_tenants):
    """POST /rfp/{id}/vendor with Tenant A's token against Tenant B's RFP must fail."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        import io
        r = await client.post(
            f"/api/v1/rfp/{t2['rfp_id']}/vendor",
            headers={"Authorization": f"Bearer {t1['token']}"},
            data={"vendor_name": "Intruder Corp"},
            files={"file": ("vendor.zip", io.BytesIO(b"PK"), "application/zip")},
        )
    assert r.status_code in (403, 404), (
        f"Expected 403 or 404, got {r.status_code}: {r.text}"
    )


@pytest.mark.asyncio
async def test_tenant_cannot_list_other_users(two_tenants):
    """GET /users with Tenant A's token must not return Tenant B's users."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {t1['token']}"},
        )
    assert r.status_code == 200
    user_ids = [u["user_id"] for u in r.json()]
    assert t2["user_id"] not in user_ids, "Tenant A must not see Tenant B's users"


@pytest.mark.asyncio
async def test_tenant_cannot_assign_evaluator_to_other_rfp(two_tenants):
    """POST /rfp/{id}/assignments with Tenant A's token against Tenant B's RFP."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{t2['rfp_id']}/assignments",
            headers={"Authorization": f"Bearer {t1['token']}"},
            json={"user_id": t1["user_id"], "evaluator_role": "technical_evaluator"},
        )
    assert r.status_code in (403, 404), (
        f"Expected 403 or 404, got {r.status_code}: {r.text}"
    )


@pytest.mark.asyncio
async def test_tenant_cannot_transition_other_rfp(two_tenants):
    """POST /rfp/{id}/transition with Tenant A's token against Tenant B's RFP."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.post(
            f"/api/v1/rfp/{t2['rfp_id']}/transition",
            headers={"Authorization": f"Bearer {t1['token']}"},
            json={"to": "criteria_approved"},
        )
    assert r.status_code in (400, 403, 404), (
        f"Expected failure, got {r.status_code}: {r.text}"
    )


@pytest.mark.asyncio
async def test_tenant_cannot_view_other_audit_log(two_tenants):
    """GET /audit-log scoped to Tenant A must not return Tenant B's entries."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            f"/api/v1/audit-log?rfp_id={t2['rfp_id']}",
            headers={"Authorization": f"Bearer {t1['token']}"},
        )
    # Either 0 results or 404 — never the other tenant's data
    if r.status_code == 200:
        assert r.json()["total"] == 0, (
            "Tenant A must see 0 audit entries for Tenant B's RFP"
        )
    else:
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_tenant_cannot_delete_other_rfp(two_tenants):
    """DELETE /rfp/{id} with Tenant A's token against Tenant B's RFP."""
    t1 = two_tenants["t1"]
    t2 = two_tenants["t2"]

    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.delete(
            f"/api/v1/rfp/{t2['rfp_id']}",
            headers={"Authorization": f"Bearer {t1['token']}"},
        )
    assert r.status_code == 404, (
        f"Tenant A must not be able to delete Tenant B's RFP. Got {r.status_code}"
    )

    # Verify Tenant B's RFP still exists
    async with AsyncSessionLocal() as db:
        rfp = (
            await db.execute(
                text(f"SELECT rfp_id FROM rfp_package WHERE rfp_id = '{t2['rfp_id']}'")
            )
        ).fetchone()
    assert rfp is not None, "Tenant B's RFP was deleted by Tenant A — isolation FAILED"


# ── Authentication required on every RFP-scoped endpoint ─────────────────────

_ZERO = "00000000-0000-0000-0000-000000000000"

_TOKEN_REQUIRED = [
    ("GET",    "/api/v1/rfp/{rfp}"),
    ("DELETE", "/api/v1/rfp/{rfp}"),
    ("DELETE", "/api/v1/rfp/{rfp}/vendor/" + _ZERO),
    ("GET",    "/api/v1/rfp/{rfp}/config"),
    ("POST",   "/api/v1/rfp/{rfp}/config"),
    ("GET",    "/api/v1/rfp/{rfp}/compare"),
    ("GET",    "/api/v1/rfp/{rfp}/vendor/" + _ZERO + "/score"),
    ("POST",   "/api/v1/rfp/{rfp}/vendor/" + _ZERO + "/score"),
    ("GET",    "/api/v1/rfp/{rfp}/vendor/" + _ZERO + "/coverage"),
    ("GET",    "/api/v1/rfp/{rfp}/vendor/" + _ZERO + "/team"),
    ("POST",   "/api/v1/rfp/{rfp}/report"),
    ("GET",    "/api/v1/rfp/{rfp}/rubric"),
    ("PATCH",  "/api/v1/rfp/{rfp}/rubric"),
    ("POST",   "/api/v1/rfp/{rfp}/rubric/lock"),
    ("GET",    "/api/v1/rfp/{rfp}/rubric/status"),
    ("GET",    "/api/v1/rfp/{rfp}/vendor/" + _ZERO + "/review"),
    ("PATCH",  "/api/v1/rfp/{rfp}/vendor/" + _ZERO + "/review/" + _ZERO),
    ("GET",    "/api/v1/rfp/{rfp}/conflicts"),
    ("GET",    "/api/v1/reports/" + _ZERO + "/status"),
    ("GET",    "/api/v1/reports/" + _ZERO + "/download"),
    ("GET",    "/api/v1/tasks/" + _ZERO),
    ("GET",    "/api/v1/audit-log"),
    ("GET",    "/api/v1/db/pool-status"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", _TOKEN_REQUIRED)
async def test_endpoint_requires_token(two_tenants, method, path):
    """No Authorization header → 401 (so the frontend redirects to login)."""
    url = path.format(rfp=two_tenants["t2"]["rfp_id"])
    kwargs = {"json": {}} if method in ("POST", "PATCH") else {}
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.request(method, url, **kwargs)
    assert r.status_code == 401, f"{method} {path} without a token returned {r.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/v1/rfp/{rfp}/config",
    "/api/v1/rfp/{rfp}/compare",
    "/api/v1/rfp/{rfp}/rubric",
    "/api/v1/rfp/{rfp}/rubric/status",
    "/api/v1/rfp/{rfp}/conflicts",
])
async def test_tenant_cannot_read_other_rfp_subresources(two_tenants, path):
    """Tenant A's token against Tenant B's RFP sub-resources → 404."""
    t1, t2 = two_tenants["t1"], two_tenants["t2"]
    async with AsyncClient(app=app, base_url=BASE) as client:
        r = await client.get(
            path.format(rfp=t2["rfp_id"]),
            headers={"Authorization": f"Bearer {t1['token']}"},
        )
    assert r.status_code == 404, f"{path} leaked Tenant B's data to Tenant A ({r.status_code})"


@pytest.mark.asyncio
async def test_chat_websocket_rejects_missing_token():
    """The chat socket must refuse the handshake without a valid token."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    tc = TestClient(app)  # no context manager → startup (Milvus connect) is skipped
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(f"/ws/chat/{_ZERO}"):
            pass
    assert exc.value.code == 4401
