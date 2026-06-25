"""Tests that ArticleVersionResponse carries the ExtractionRun.version label.

Exercises the async FastAPI routes end-to-end via httpx.AsyncClient with get_db
overridden to point at docextractor_test.
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


async def test_article_versions_carry_run_version(client):
    client, TestSession = client
    from app.models import Vendor, Product, DocumentationSource, Article, ArticleVersion
    from app.models.extraction_run import ExtractionRun, RunStatus

    async with TestSession() as s:
        v = Vendor(name="V2")
        s.add(v)
        await s.flush()
        p = Product(vendor_id=v.id, name="P2", version="11.0")
        s.add(p)
        await s.flush()
        src = DocumentationSource(
            product_id=p.id,
            name="S2",
            base_url="https://x/11.0/a",
        )
        s.add(src)
        await s.flush()
        run = ExtractionRun(source_id=src.id, status=RunStatus.COMPLETED, version="11.0")
        s.add(run)
        await s.flush()
        art = Article(
            source_id=src.id,
            title="A",
            source_url="https://x/11.0/a",
            topic_key="https://x/{version}/a",
            content_markdown="now",
        )
        s.add(art)
        await s.flush()
        ver = ArticleVersion(
            article_id=art.id,
            extraction_run_id=run.id,
            content_markdown="old",
            content_hash="h",
        )
        s.add(ver)
        await s.commit()
        aid = art.id

    r = await client.get(f"/api/articles/{aid}/versions")
    assert r.status_code == 200
    assert r.json()["versions"][0]["version"] == "11.0"
