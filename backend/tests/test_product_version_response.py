"""Tests for product version fields on ProductResponse.

Tests that the version and previous_version fields are exposed on the ProductResponse schema.
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
from app.models import Vendor, Product

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

pytestmark = pytest.mark.asyncio


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


async def test_product_response_includes_version_fields(client):
    """Test that ProductResponse includes version and previous_version fields."""
    c, db_session = client

    # Seed a vendor + product with version set, then GET it and assert the fields.
    async with db_session() as s:
        v = Vendor(name="Vv")
        s.add(v)
        await s.flush()
        p = Product(
            vendor_id=v.id,
            name="Pp",
            version="10.0",
            previous_version="9.0"
        )
        s.add(p)
        await s.commit()
        pid = p.id

    r = await c.get(f"/api/products/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "10.0"
    assert body["previous_version"] == "9.0"
