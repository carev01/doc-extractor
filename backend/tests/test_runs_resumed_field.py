"""/api/extraction/runs exposes articles_resumed."""
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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun

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


async def _run(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="D", base_url="https://d")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, articles_resumed=7, articles_total=10)
        s.add(run); await s.commit()
        return run.id


async def test_list_runs_includes_articles_resumed(client):
    c, factory = client
    rid = await _run(factory)
    body = (await c.get("/api/extraction/runs")).json()
    row = next(r for r in body["runs"] if r["id"] == str(rid))
    assert row["articles_resumed"] == 7


async def test_run_status_includes_articles_resumed(client):
    c, factory = client
    rid = await _run(factory)
    body = (await c.get(f"/api/extraction/runs/{rid}")).json()
    assert body["articles_resumed"] == 7
