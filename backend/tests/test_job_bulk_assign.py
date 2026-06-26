"""PUT /api/jobs/{id}/sources assigns/reassigns many sources at once."""
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


async def _source(factory, name) -> uuid.UUID:
    sfx = uuid.uuid4().hex[:8]
    async with factory() as s:
        v = Vendor(name=f"V-{sfx}"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name=f"P-{sfx}"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name=name, base_url=f"https://d/{sfx}")
        s.add(src); await s.commit()
        return src.id


async def test_bulk_assign_multiple_sources(client):
    c, factory = client
    s1 = await _source(factory, "A")
    s2 = await _source(factory, "B")
    job = (await c.post("/api/jobs", json={"name": "J"})).json()

    resp = await c.put(f"/api/jobs/{job['id']}/sources",
                       json={"source_ids": [str(s1), str(s2)]})
    assert resp.status_code == 200
    assert resp.json()["source_count"] == 2


async def test_bulk_assign_reassigns_from_other_job(client):
    c, factory = client
    s1 = await _source(factory, "A")
    j1 = (await c.post("/api/jobs", json={"name": "J1"})).json()
    j2 = (await c.post("/api/jobs", json={"name": "J2"})).json()
    await c.put(f"/api/jobs/{j1['id']}/sources", json={"source_ids": [str(s1)]})
    await c.put(f"/api/jobs/{j2['id']}/sources", json={"source_ids": [str(s1)]})

    assert (await c.get(f"/api/jobs/{j1['id']}")).json()["source_count"] == 0
    assert (await c.get(f"/api/jobs/{j2['id']}")).json()["source_count"] == 1


async def test_bulk_assign_unknown_source_is_404(client):
    c, _ = client
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    resp = await c.put(f"/api/jobs/{job['id']}/sources",
                       json={"source_ids": [str(uuid.uuid4())]})
    assert resp.status_code == 404
