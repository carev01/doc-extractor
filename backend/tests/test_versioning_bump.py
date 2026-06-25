"""Tests for POST /api/products/{id}/versions/bump.

Exercises the version-bump route that rewrites base_url on templated sources,
updates product.version / previous_version, and enqueues one pending run per
affected source.

Uses the same (client, session_factory) harness as test_versioning_enable.py.
"""

import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
from app.models import DocumentationSource, ExtractionRun, Product, Vendor

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


@pytest_asyncio.fixture
async def seeded_templated_product(client):
    """Seed: vendor → product (version=10.0) → one templated source.

    The source's url_template contains {version}; base_url is resolved to 10.0.
    """
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="BumpCo")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="BumpTest", version="10.0")
        s.add(product)
        await s.flush()
        source = DocumentationSource(
            product_id=product.id,
            name="BumpTest Docs",
            base_url="https://docs.example.com/Available/10.0/BumpTest/a.htm",
            url_template="https://docs.example.com/Available/{version}/BumpTest/a.htm",
        )
        s.add(source)
        await s.commit()
        await s.refresh(product)
        return product


@pytest_asyncio.fixture
async def seeded_product_plain(client):
    """Seed: vendor → product → one source with NO url_template."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="PlainCo")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="PlainTest", version="1.0")
        s.add(product)
        await s.flush()
        source = DocumentationSource(
            product_id=product.id,
            name="PlainTest Docs",
            base_url="https://docs.example.com/guide",
            # url_template intentionally left None
        )
        s.add(source)
        await s.commit()
        await s.refresh(product)
        return product


async def test_bump_rewrites_urls_and_enqueues_runs(client, seeded_templated_product):
    """POST bump {version:11.0} rewrites base_url, updates product, enqueues run."""
    c, session_factory = client
    r = await c.post(
        f"/api/products/{seeded_templated_product.id}/versions/bump",
        json={"version": "11.0"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "11.0" and len(body["runs"]) == 1

    async with session_factory() as db_session:
        prod = await db_session.get(Product, seeded_templated_product.id)
        assert prod.version == "11.0" and prod.previous_version == "10.0"

        src = (
            await db_session.execute(
                select(DocumentationSource).where(
                    DocumentationSource.product_id == prod.id
                )
            )
        ).scalar_one()
        assert "/11.0/" in src.base_url

        run = (
            await db_session.execute(
                select(ExtractionRun).where(ExtractionRun.source_id == src.id)
            )
        ).scalar_one()
        assert run.status.value == "pending"


async def test_bump_rejects_when_no_templated_sources(client, seeded_product_plain):
    """A product with no url_template sources → 400."""
    c, _ = client
    r = await c.post(
        f"/api/products/{seeded_product_plain.id}/versions/bump",
        json={"version": "2.0"},
    )
    assert r.status_code == 400
