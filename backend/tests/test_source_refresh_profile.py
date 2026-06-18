"""Tests for the refresh_profile flag on PATCH /api/sources/{source_id}.

Uses the same async AsyncClient + NullPool pattern as test_schedule_routes.py.
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
async def test_refresh_profile_clears_llm_spec_retains_other(client):
    """refresh_profile=True removes llm_spec but preserves other profile_config keys."""
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s = DocumentationSource(
            vendor_id=v.id,
            name="S",
            base_url="http://example.com",
            profile_config={
                "llm_spec": {"strategy": "sidebar", "nav_selector": "#t"},
                "other": 1,
            },
        )
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r = await c.patch(f"/api/sources/{sid}", json={"refresh_profile": True})
    assert r.status_code == 200

    # Verify persisted state via a fresh GET.
    async with sf() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == s.id)
        )).scalar_one()
        assert row.profile_config is not None
        assert "llm_spec" not in row.profile_config
        assert row.profile_config.get("other") == 1


@pytest.mark.asyncio
async def test_refresh_profile_null_profile_config_is_noop(client):
    """refresh_profile=True when profile_config is NULL must not error."""
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V2"); db.add(v); await db.flush()
        s = DocumentationSource(
            vendor_id=v.id,
            name="S2",
            base_url="http://example2.com",
            profile_config=None,
        )
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r = await c.patch(f"/api/sources/{sid}", json={"refresh_profile": True})
    assert r.status_code == 200

    async with sf() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == s.id)
        )).scalar_one()
        assert row.profile_config is None


@pytest.mark.asyncio
async def test_refresh_profile_only_llm_spec_sets_profile_config_to_null(client):
    """When llm_spec is the only key, clearing it sets profile_config to None."""
    c, sf = client
    async with sf() as db:
        v = Vendor(name="V3"); db.add(v); await db.flush()
        s = DocumentationSource(
            vendor_id=v.id,
            name="S3",
            base_url="http://example3.com",
            profile_config={"llm_spec": {"strategy": "sidebar", "nav_selector": "#nav"}},
        )
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r = await c.patch(f"/api/sources/{sid}", json={"refresh_profile": True})
    assert r.status_code == 200

    async with sf() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == s.id)
        )).scalar_one()
        assert row.profile_config is None
