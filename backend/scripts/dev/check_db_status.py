
import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.db import RFPPackage, VendorResponse, Task

async def check_db():
    async with AsyncSessionLocal() as db:
        # Check RFPs
        rfps = (await db.execute(select(RFPPackage))).scalars().all()
        print(f"RFPs found: {len(rfps)}")
        for r in rfps:
            print(f"  RFP: {r.rfp_id} | Title: {r.title} | Status: {r.status}")
        
        # Check Vendor Responses
        responses = (await db.execute(select(VendorResponse))).scalars().all()
        print(f"Vendor Responses found: {len(responses)}")
        for r in responses:
            print(f"  Response: {r.response_id} | RFP: {r.rfp_id} | Status: {r.status}")

        # Check Recent Tasks
        tasks = (await db.execute(select(Task).order_by(Task.created_at.desc()).limit(10))).scalars().all()
        print(f"Recent Tasks:")
        for t in tasks:
            print(f"  [{t.created_at}] Task: {t.task_id} | Desc: {t.description} | Status: {t.status}")
            if t.status == 'error':
                print(f"    Error: {str(t.error)[:200]}...")

if __name__ == "__main__":
    asyncio.run(check_db())
