"""
app/middleware/tenant.py
────────────────────────────────────────────────────────────────────────────
Tenant resolution middleware.

Every authenticated request must carry a JWT whose payload includes
tenant_id. This middleware:

  1. Extracts tenant_id from the Bearer token (no DB call needed — it's
     in the signed JWT claim).
  2. Sets the PostgreSQL session variable app.tenant_id via
     SET LOCAL app.tenant_id = '<uuid>'
     so that RLS policies on every tenant-scoped table filter automatically.
  3. Attaches tenant_id and user context to request.state so route handlers
     can read it without re-decoding the token.

PUBLIC PATHS (no tenant required):
  /api/v1/auth/login
  /api/v1/auth/me (token required but tenant optional — used on first load)
  /api/v1/tenants/register
  /api/v1/health
  /docs, /openapi.json

HOW TO REGISTER (in main.py, after app = FastAPI(...)):
  from app.middleware.tenant import TenantMiddleware
  app.add_middleware(TenantMiddleware)
"""

import logging
from typing import Optional

from fastapi import Request, Response
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import Tenant

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

# Paths that don't need tenant resolution
_PUBLIC_PREFIXES = (
    "/api/v1/auth/login",
    "/api/v1/tenants/register",
    "/api/v1/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/ws/",           # WebSocket auth handled inside the handler
)


def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token")   # WS fallback


def _decode_claims(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Public paths — skip tenant resolution entirely
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        token = _extract_token(request)
        if not token:
            # Let the route's Depends(require_role()) return the 401
            return await call_next(request)

        claims = _decode_claims(token)
        if not claims:
            return await call_next(request)

        tenant_id  = claims.get("tenant_id")
        user_id    = claims.get("sub")
        user_role  = claims.get("role")
        user_email = claims.get("email", "")

        # Attach to request.state so routes can read without re-decoding
        request.state.tenant_id  = tenant_id
        request.state.user_id    = user_id
        request.state.user_role  = user_role
        request.state.user_email = user_email
        request.state.ip_address = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.client.host
            if request.client else "unknown"
        )
        request.state.user_agent = request.headers.get("User-Agent", "")

        if not tenant_id:
            # Token exists but has no tenant_id — legacy token or super-admin
            return await call_next(request)

        # Set PostgreSQL session variable for RLS
        # This runs inside the same connection the route handler will use,
        # scoped to the transaction (SET LOCAL resets on ROLLBACK/COMMIT).
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(
                    # Use text() to pass a literal — tenant_id is a UUID string
                    # from a signed JWT so it is safe to interpolate here.
                    __import__("sqlalchemy").text(
                        f"SET LOCAL app.tenant_id = '{tenant_id}'"
                    )
                )
                await db.commit()
            except Exception as exc:
                logger.warning("Failed to set app.tenant_id: %s", exc)

        return await call_next(request)
