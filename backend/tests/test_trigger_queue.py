"""Tests for the refactored trigger route (enqueue-based).

The trigger route now enqueues a pending run via enqueue_run instead of
running extraction in-process via BackgroundTask.
"""

import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, DocumentationSource

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def client():
    """Yield (AsyncClient, session_factory).

    Per-test NullPool engine so connections bind to this test's event loop.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_trigger_enqueues_pending_run(client):
    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r1 = await c.post(f"/api/extraction/trigger/{sid}")
    assert r1.status_code == 200
    assert r1.json()["status"] == "pending"

    # Second trigger while one is active -> 409 (coalesced by the DB invariant).
    r2 = await c.post(f"/api/extraction/trigger/{sid}")
    assert r2.status_code == 409

    runs = await c.get(f"/api/extraction/runs?source_id={sid}")
    assert runs.json()["runs"][0]["trigger"] == "manual"
