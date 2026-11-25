
import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.db import Task

async def get_failed_tasks():
    async with AsyncSessionLocal() as db:
        stmt = select(Task).where(Task.status == 'error').order_by(Task.created_at.desc()).limit(5)
        result = await db.execute(stmt)
        tasks = result.scalars().all()
        for t in tasks:
            print(f"Task ID: {t.task_id}")
            print(f"Description: {t.description}")
            print(f"Error: {t.error}")
            print(f"Created At: {t.created_at}")
            print("-" * 20)

if __name__ == "__main__":
    asyncio.run(get_failed_tasks())
