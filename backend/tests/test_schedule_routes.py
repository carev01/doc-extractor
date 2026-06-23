"""Tests for per-source schedule CRUD routes.

Uses the same async client fixture pattern as test_versions.py:
per-test NullPool engine, get_db override, httpx.AsyncClient.
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
from app.models import (
    Vendor,
    DocumentationSource,
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def client():
    """Yield (AsyncClient, session_factory).

    The async engine is created per-test with NullPool so its connections bind
    to this test's event loop — pytest-asyncio gives each test its own loop, and
    a shared pooled engine would raise "another operation is in progress".
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


@pytest.mark.asyncio
async def test_get_schedule_404_when_none(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s_prod = Product(vendor_id=v.id, name="P")
        db.add(s_prod)
        await db.flush()
        s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    r = await c.get(f"/api/sources/{sid}/schedule")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_schedule_builds_cron_and_next_run(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s_prod = Product(vendor_id=v.id, name="P")
        db.add(s_prod)
        await db.flush()
        s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    body = {"enabled": True, "frequency": "daily", "time_of_day": "02:00", "timezone": "UTC"}
    r = await c.put(f"/api/sources/{sid}/schedule", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["cron"] == "0 2 * * *"
    assert data["enabled"] is True
    assert data["next_run_at"] is not None

    # Round-trips via GET, and PUT again upserts (no duplicate row error).
    g = await c.get(f"/api/sources/{sid}/schedule")
    assert g.json()["frequency"] == "daily"
    r2 = await c.put(f"/api/sources/{sid}/schedule",
                     json={**body, "frequency": "weekly", "day_of_week": 0})
    assert r2.json()["cron"] == "0 2 * * 0"


@pytest.mark.asyncio
async def test_disabled_schedule_has_null_next_run(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s_prod = Product(vendor_id=v.id, name="P")
        db.add(s_prod)
        await db.flush()
        s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    body = {"enabled": False, "frequency": "daily", "time_of_day": "02:00", "timezone": "UTC"}
    r = await c.put(f"/api/sources/{sid}/schedule", json=body)
    assert r.json()["next_run_at"] is None


@pytest.mark.asyncio
async def test_delete_schedule(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s_prod = Product(vendor_id=v.id, name="P")
        db.add(s_prod)
        await db.flush()
        s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    await c.put(f"/api/sources/{sid}/schedule",
                json={"enabled": True, "frequency": "daily", "time_of_day": "02:00", "timezone": "UTC"})
    d = await c.delete(f"/api/sources/{sid}/schedule")
    assert d.status_code == 204
    assert (await c.get(f"/api/sources/{sid}/schedule")).status_code == 404


@pytest.mark.asyncio
async def test_put_schedule_rejects_bad_time(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s_prod = Product(vendor_id=v.id, name="P")
        db.add(s_prod)
        await db.flush()
        s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)
    r = await c.put(f"/api/sources/{sid}/schedule",
                    json={"enabled": True, "frequency": "daily", "time_of_day": "99:99", "timezone": "UTC"})
    assert r.status_code == 422
