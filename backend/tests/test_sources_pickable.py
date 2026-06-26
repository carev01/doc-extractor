"""GET /api/sources/pickable returns labelled sources with current job."""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource
from app.models.job import Job

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_pickable_lists_sources_with_labels_and_job(client):
    c, factory = client
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()
        job = Job(name="Nightly"); s.add(job); await s.flush()
        s.add(DocumentationSource(product_id=p.id, name="Guide",
                                  base_url="https://d/1", job_id=job.id))
        s.add(DocumentationSource(product_id=p.id, name="API",
                                  base_url="https://d/2"))
        await s.commit()

    body = (await c.get("/api/sources/pickable")).json()
    rows = {r["name"]: r for r in body["sources"]}
    assert rows["Guide"]["vendor_name"] == "Acme"
    assert rows["Guide"]["product_name"] == "Cloud"
    assert rows["Guide"]["job_name"] == "Nightly"
    assert rows["API"]["job_id"] is None
    assert rows["API"]["job_name"] is None
