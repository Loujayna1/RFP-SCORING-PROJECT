"""
Shared pytest configuration for async tests.

Key fix: pytest-asyncio defaults to creating a new event loop per test.
SQLAlchemy's connection pool (asyncpg) binds connections to the loop that
created them. When a new loop appears, those pooled connections raise
"Future attached to a different loop".

Solution: use a single event loop for the entire test session and dispose
the app's engine pool after all tests complete.

Test database
-------------
Tests never touch the application database. Before any app module is
imported, DATABASE_URL is pointed at TEST_DATABASE_URL (default: the app
database name with a "_test" suffix). That database is created if missing
and migrated to head once per session.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest


def _configure_test_database() -> None:
    from dotenv import dotenv_values

    app_url = os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL")
    if not app_url:
        app_url = "postgresql+asyncpg://rfp:rfp_pass@localhost:5432/rfp_evaluator"

    parts = urlsplit(app_url)
    app_db = parts.path.lstrip("/")
    test_url = os.environ.get("TEST_DATABASE_URL") or urlunsplit(parts._replace(path=f"/{app_db}_test"))
    test_db = urlsplit(test_url).path.lstrip("/")
    if test_db == app_db:
        raise RuntimeError(f"Refusing to run tests against the application database '{app_db}'")

    # Create the test database if it does not exist yet
    import psycopg2
    admin = urlsplit(test_url.replace("+asyncpg", ""))._replace(path="/postgres")
    conn = psycopg2.connect(urlunsplit(admin))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{test_db}"')
    conn.close()

    os.environ["DATABASE_URL"] = test_url
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=os.environ.copy())


_configure_test_database()

# App imports must come after DATABASE_URL points at the test database
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    """
    Override the default per-test event loop with a session-scoped one.
    All async tests share the same loop, avoiding cross-loop pool conflicts.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True, scope="session")
async def _dispose_engine_pool():
    """Dispose the app engine pool after the test session ends."""
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _rate_limiter():
    """
    Initialise FastAPILimiter (normally done in the app startup event, which
    the test client does not run). Every request gets a unique identifier so
    tests never trip the per-route limits. Function-scoped so the Redis client
    lives on the same event loop as the test.
    """
    import redis.asyncio as redis
    from fastapi_limiter import FastAPILimiter

    async def _unique_identifier(request) -> str:
        return str(uuid.uuid4())

    r = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    await FastAPILimiter.init(r, identifier=_unique_identifier)
    yield
    await FastAPILimiter.close()


async def purge_tenant(tenant_id: str) -> None:
    """
    Delete every row belonging to a test tenant. Runs with
    session_replication_role=replica so FK checks and the audit_log
    immutability trigger are skipped (test database only).
    """
    rfps  = "SELECT rfp_id FROM rfp_package WHERE tenant_id = CAST(:t AS uuid)"
    resps = "SELECT response_id FROM vendor_response WHERE tenant_id = CAST(:t AS uuid)"
    docs  = f"SELECT doc_id FROM document WHERE tenant_id = CAST(:t AS uuid) OR rfp_id IN ({rfps})"
    sessions = f"SELECT session_id FROM chat_session WHERE tenant_id = CAST(:t AS uuid) OR rfp_id IN ({rfps})"

    statements = [
        # Child tables without tenant_id first, while their parents still exist
        f"DELETE FROM chat_message WHERE session_id IN ({sessions})",
        f"DELETE FROM ocr_result WHERE doc_id IN ({docs})",
        f"DELETE FROM doc_chunk WHERE doc_id IN ({docs})",
        f"DELETE FROM requirement_score WHERE response_id IN ({resps})",
        f"DELETE FROM team_member WHERE response_id IN ({resps})",
        f"DELETE FROM vendor_claim WHERE response_id IN ({resps})",
        f"DELETE FROM coverage_entry WHERE rfp_id IN ({rfps}) OR response_id IN ({resps})",
        f"DELETE FROM vendor_score WHERE rfp_id IN ({rfps}) OR response_id IN ({resps})",
        f"DELETE FROM requirement WHERE rfp_id IN ({rfps})",
        f"DELETE FROM evaluation_criterion WHERE rfp_id IN ({rfps})",
        f"DELETE FROM chat_session WHERE session_id IN ({sessions})",
        f"DELETE FROM document WHERE doc_id IN ({docs})",
    ] + [
        f"DELETE FROM {table} WHERE tenant_id = CAST(:t AS uuid)"
        for table in [
            "audit_log", "business_vote", "chat_artifact", "report", "rfp_assignment",
            "task", "transaction_log", "vendor_response", "vendor", "rfp_package",
            '"user"', "tenant",
        ]
    ]

    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL session_replication_role = replica"))
        for stmt in statements:
            await db.execute(text(stmt), {"t": str(tenant_id)})
        await db.commit()
