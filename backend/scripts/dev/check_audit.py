
import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.db import AuditLog

async def main():
    async with AsyncSessionLocal() as db:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20)
        logs = (await db.execute(stmt)).scalars().all()
        for l in logs:
            print(f"{l.created_at} | {l.action} | {l.entity_type} | {l.details}")

if __name__ == "__main__":
    asyncio.run(main())
