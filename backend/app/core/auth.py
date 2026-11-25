"""
app/core/auth.py — multi-tenant version
────────────────────────────────────────
Users read from DB. JWT carries tenant_id. require_role() accepts
a list of roles. Invite-accept endpoint activates new accounts.
"""
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import User, UserStatus

logger  = logging.getLogger(__name__)
router  = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)  # missing token -> 401 (below), not 403
_pwd    = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


# ── Token helpers ─────────────────────────────────────────────────────────────

def create_token(user) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {
            "sub":       str(user.user_id),
            "email":     user.email,
            "role":      user.role,
            "tenant_id": str(user.tenant_id),
            "exp":       int(expire.timestamp()),
        },
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    return decode_token(credentials.credentials if credentials else "")


def require_role(*roles: str):
    """
    Factory. Pass one or more UserRole values.
    Empty call (require_role()) allows any authenticated user.

    Usage:
        Depends(require_role("admin"))
        Depends(require_role("admin", "technical_evaluator"))
        Depends(get_current_user)   # any authenticated user
    """
    allowed = set(roles)

    async def _check(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ) -> dict:
        payload = decode_token(credentials.credentials if credentials else "")
        if allowed and payload.get("role") not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Required role(s): {', '.join(sorted(allowed))}. "
                    f"Your role: '{payload.get('role')}'."
                ),
            )
        return payload
    return _check


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email:    str
    password: str


class AcceptInviteRequest(BaseModel):
    invite_token: str
    password:     str
    display_name: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(body: LoginRequest):
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(
                select(User).where(
                    User.email  == body.email.strip().lower(),
                    User.status == UserStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()

    if not user or not user.hashed_password or not _pwd.verify(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.user_id == user.user_id))).scalar_one()
        u.last_active_at = datetime.utcnow()
        await db.commit()

    token = create_token(user)
    logger.info("Login: email=%s tenant=%s role=%s", user.email, user.tenant_id, user.role)
    return {
        "access_token": token,
        "token_type":   "bearer",
        "expires_in":   settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user_id":      str(user.user_id),
        "email":        user.email,
        "display_name": user.display_name or user.email,
        "role":         user.role,
        "tenant_id":    str(user.tenant_id),
    }


@router.get("/me")
async def me(payload: dict = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.user_id == payload["sub"]))
        ).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    return {
        "user_id":        str(user.user_id),
        "email":          user.email,
        "display_name":   user.display_name or user.email,
        "role":           user.role,
        "tenant_id":      str(user.tenant_id),
        "status":         user.status,
        "last_active_at": user.last_active_at.isoformat() if user.last_active_at else None,
    }


@router.post("/logout")
async def logout():
    return {"detail": "Logged out. Discard your token on the client side."}


@router.post("/invite/accept")
async def accept_invite(body: AcceptInviteRequest):
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(
                select(User).where(User.invite_token == body.invite_token)
            )
        ).scalar_one_or_none()

        if not user:
            raise HTTPException(400, "Invalid or expired invite token")
        if user.invite_expires_at and user.invite_expires_at < datetime.utcnow():
            raise HTTPException(400, "Invite token has expired")
        if user.status == UserStatus.ACTIVE:
            raise HTTPException(400, "Account already activated")

        user.hashed_password   = _pwd.hash(body.password)
        user.status            = UserStatus.ACTIVE
        user.accepted_at       = datetime.utcnow()
        user.invite_token      = None
        user.invite_expires_at = None
        if body.display_name:
            user.display_name  = body.display_name
        await db.commit()

        return {
            "access_token": create_token(user),
            "token_type":   "bearer",
            "expires_in":   settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "user_id":      str(user.user_id),
            "email":        user.email,
            "display_name": user.display_name or user.email,
            "role":         user.role,
            "tenant_id":    str(user.tenant_id),
        }
