
import asyncio
from datetime import datetime
from sqlalchemy import select, update
from app.db.session import AsyncSessionLocal
from app.models.db import Task, RFPPackage

async def cleanup_stale_tasks():
    async with AsyncSessionLocal() as db:
        # Mark all 'running' tasks as 'error' because the process was likely restarted
        stmt = update(Task).where(Task.status == 'running').values(
            status='error',
            error='Task was interrupted by a system restart or crash.',
            completed_at=datetime.utcnow()
        )
        await db.execute(stmt)
        
        # Also mark RFPs that are 'ingesting' as 'error' if they have no running tasks
        # (Simplified: just mark all 'ingesting' or 'ready' but stuck ones as error if needed)
        # For now, just tasks is enough as the UI usually polls tasks.
        
        await db.commit()
        print("Cleanup complete.")

if __name__ == "__main__":
    asyncio.run(cleanup_stale_tasks())
