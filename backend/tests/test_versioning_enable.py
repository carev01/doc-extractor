"""Tests for POST /api/products/{id}/versions/enable.

Exercises the enable-versioning route that templatizes child sources and
rekeys their existing articles with a version-independent topic_key.

Uses the same httpx.AsyncClient + get_db-override harness as test_products.py.
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
from tests.db_reset import reset_schema
from app.main import app
from app.models import Article, DocumentationSource, Product, Vendor

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(reset_schema)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()

    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_product_10(client):
    """Seed vendor → product (no version yet) → source with versioned base_url
    + one article whose topic_key currently equals its full 10.0 source_url."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="VersionCo")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="EnableTest")
        s.add(product)
        await s.flush()
        source = DocumentationSource(
            product_id=product.id,
            name="EnableTest Docs",
            base_url="https://docs.example.com/Available/10.0/EnableTest/default.htm",
        )
        s.add(source)
        await s.flush()
        article_url = "https://docs.example.com/Available/10.0/EnableTest/page1.htm"
        article = Article(
            source_id=source.id,
            title="Page 1",
            source_url=article_url,
            topic_key=article_url,  # pre-enable: topic_key equals full URL
            content_markdown="# Page 1",
        )
        s.add(article)
        await s.commit()
        await s.refresh(product)
        return product


async def test_enable_templatizes_and_rekeys(client, seeded_product_10):
    """POST enable {version:10.0} templatizes the source and rekeys the article."""
    c, session_factory = client
    r = await c.post(
        f"/api/products/{seeded_product_10.id}/versions/enable",
        json={"version": "10.0"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "10.0"
    assert body["templatized_sources"] == 1

    async with session_factory() as db_session:
        src = (
            await db_session.execute(
                select(DocumentationSource).where(
                    DocumentationSource.product_id == seeded_product_10.id
                )
            )
        ).scalar_one()
        assert "{version}" in src.url_template

        art = (
            await db_session.execute(
                select(Article).where(Article.source_id == src.id)
            )
        ).scalar_one()
        assert "{version}" in art.topic_key  # rekeyed, ready for a future bump


async def test_enable_unknown_product_404(client):
    """POST enable for a non-existent product returns 404."""
    import uuid
    c, _ = client
    r = await c.post(
        f"/api/products/{uuid.uuid4()}/versions/enable",
        json={"version": "10.0"},
    )
    assert r.status_code == 404


async def test_enable_skips_sources_without_version_in_url(client, seeded_product_10):
    """Sources whose base_url does not contain the version are left untouched."""
    c, session_factory = client

    # Add a second source without the version in its URL
    async with session_factory() as s:
        unversioned_source = DocumentationSource(
            product_id=seeded_product_10.id,
            name="Unversioned Docs",
            base_url="https://docs.example.com/latest/guide",
        )
        s.add(unversioned_source)
        await s.commit()
        unversioned_id = unversioned_source.id

    r = await c.post(
        f"/api/products/{seeded_product_10.id}/versions/enable",
        json={"version": "10.0"},
    )
    assert r.status_code == 200
    assert r.json()["templatized_sources"] == 1  # only the versioned source

    async with session_factory() as s:
        unversioned = await s.get(DocumentationSource, unversioned_id)
        # url_template should remain None (untouched)
        assert unversioned.url_template is None


async def test_enable_keeps_pdf_heading_path_keys(client, seeded_product_10):
    """A PDF source is templatized but its articles keep their heading-path keys.

    PDF articles are keyed by their section path (derive_pdf_topic_key), not by
    URL, and the import matches on that key. Re-keying them from source_url would
    make the next import retire and re-add every section of the PDF."""
    c, session_factory = client
    pdf_url = "https://docs.example.com/Available/10.0/Guide.pdf"
    async with session_factory() as s:
        pdf_source = DocumentationSource(
            product_id=seeded_product_10.id,
            name="PDF Guide",
            base_url=pdf_url,
            source_type="pdf",
        )
        s.add(pdf_source)
        await s.flush()
        s.add(Article(
            source_id=pdf_source.id,
            title="Upgrading",
            source_url=pdf_url + "#page=17&s=3",
            topic_key="upgrading-to-10-0",
            content_markdown="# Upgrading",
        ))
        await s.commit()
        pdf_id = pdf_source.id

    r = await c.post(
        f"/api/products/{seeded_product_10.id}/versions/enable",
        json={"version": "10.0"},
    )
    assert r.status_code == 200
    assert r.json()["templatized_sources"] == 2

    async with session_factory() as s:
        src = await s.get(DocumentationSource, pdf_id)
        assert src.url_template == "https://docs.example.com/Available/{version}/Guide.pdf"
        art = (
            await s.execute(select(Article).where(Article.source_id == pdf_id))
        ).scalar_one()
        assert art.topic_key == "upgrading-to-10-0"
