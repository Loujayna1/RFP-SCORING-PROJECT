
import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.db import Task, RFPPackage

async def main():
    async with AsyncSessionLocal() as db:
        stmt = select(Task).order_by(Task.created_at.desc()).limit(1)
        task = (await db.execute(stmt)).scalar_one_or_none()
        if task:
            print(f"Task ID: {task.task_id}")
            print(f"Status: {task.status}")
            print(f"Error: {task.error}")
            print(f"Result: {task.result}")
        else:
            print("No tasks found")

        stmt = select(RFPPackage).order_by(RFPPackage.created_at.desc()).limit(1)
        rfp = (await db.execute(stmt)).scalar_one_or_none()
        if rfp:
            print(f"Last RFP ID: {rfp.rfp_id}")
            print(f"Last RFP Status: {rfp.status}")

if __name__ == "__main__":
    asyncio.run(main())
