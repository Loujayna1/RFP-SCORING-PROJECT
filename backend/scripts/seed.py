"""
First-run seed: create the initial tenant + admin user so you can log in.

Usage (from the backend container or a shell with the app env):

    python -m scripts.seed
    # or override the defaults:
    SEED_ADMIN_EMAIL=you@example.com SEED_ADMIN_PASSWORD='strong-pass' \
        SEED_COMPANY='My Company' python -m scripts.seed

Idempotent: if an account with the email already exists, it does nothing.
After it runs, sign in at the frontend with the printed email + password.
"""
import asyncio
import os
import uuid
from datetime import datetime

from passlib.context import CryptContext
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.db import Tenant, User, UserRole, UserStatus

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

EMAIL    = os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "ChangeMe!2026")
COMPANY  = os.environ.get("SEED_COMPANY", "Demo Company")


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "tenant"


async def main() -> None:
    email = EMAIL.strip().lower()
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing:
            print(f"✓ Account already exists for {email} — nothing to do.")
            return

        tenant_id = str(uuid.uuid4())
        db.add(Tenant(
            tenant_id=tenant_id,
            name=COMPANY.strip(),
            slug=_slug(COMPANY),
            plan="pro",
            status="active",
        ))
        await db.flush()
        db.add(User(
            user_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            email=email,
            hashed_password=_pwd.hash(PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            accepted_at=datetime.utcnow(),
        ))
        await db.commit()

    print("✓ Seeded first tenant + admin.")
    print(f"    Company:  {COMPANY}")
    print(f"    Email:    {email}")
    print(f"    Password: {PASSWORD}")
    print("  Sign in with these, then change the password.")


if __name__ == "__main__":
    asyncio.run(main())
