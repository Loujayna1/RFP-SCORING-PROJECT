
import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.db import RFPPackage, VendorResponse, Task

async def check_status():
    async with AsyncSessionLocal() as db:
        print("--- RFPs ---")
        rfps = (await db.execute(select(RFPPackage))).scalars().all()
        for r in rfps:
            print(f"ID: {r.rfp_id} | Title: {r.title} | Status: {r.status}")
        
        print("\n--- Vendor Responses ---")
        vrs = (await db.execute(select(VendorResponse))).scalars().all()
        for v in vrs:
            print(f"ID: {v.response_id} | RFP: {v.rfp_id} | Status: {v.status}")

        print("\n--- Tasks ---")
        tasks = (await db.execute(select(Task).order_by(Task.created_at.desc()).limit(10))).scalars().all()
        for t in tasks:
            print(f"ID: {t.task_id} | Status: {t.status} | Error: {t.error}")

if __name__ == "__main__":
    asyncio.run(check_status())
