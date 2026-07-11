"""GET /api/dashboard/enrichment — per-source and corpus image-enrichment stats."""
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.extraction_run import RunStatus
from app.models.image import ArticleImage

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ctx():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_two_sources(factory):
    """A: 2 described, 3 pending, 1 decorative; B: all described + active run."""
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()

        src_a = DocumentationSource(name="A", base_url="https://a", product_id=p.id)
        src_b = DocumentationSource(name="B", base_url="https://b", product_id=p.id)
        s.add_all([src_a, src_b]); await s.flush()

        art_a = Article(
            source_id=src_a.id, title="Aart", source_url="https://a/x", topic_key="https://a/x",
            content_markdown="# A", content_hash="h-a",
        )
        art_b = Article(
            source_id=src_b.id, title="Bart", source_url="https://b/x", topic_key="https://b/x",
            content_markdown="# B", content_hash="h-b",
        )
        s.add_all([art_a, art_b]); await s.flush()

        images_a = [
            # 2 described
            ArticleImage(
                article_id=art_a.id, original_url="https://a/1.png",
                local_filename="1.png", local_path="/tmp/1.png",
                description="d1", is_meaningful=True,
            ),
            ArticleImage(
                article_id=art_a.id, original_url="https://a/2.png",
                local_filename="2.png", local_path="/tmp/2.png",
                description="d2", is_meaningful=True,
            ),
            # 3 pending: mix of is_meaningful IS NULL and is_meaningful=True/description NULL
            ArticleImage(
                article_id=art_a.id, original_url="https://a/3.png",
                local_filename="3.png", local_path="/tmp/3.png",
                description=None, is_meaningful=None,
            ),
            ArticleImage(
                article_id=art_a.id, original_url="https://a/4.png",
                local_filename="4.png", local_path="/tmp/4.png",
                description=None, is_meaningful=None,
            ),
            ArticleImage(
                article_id=art_a.id, original_url="https://a/5.png",
                local_filename="5.png", local_path="/tmp/5.png",
                description=None, is_meaningful=True,
            ),
            # 1 decorative — excluded from both described and pending
            ArticleImage(
                article_id=art_a.id, original_url="https://a/6.png",
                local_filename="6.png", local_path="/tmp/6.png",
                description=None, is_meaningful=False,
            ),
        ]
        images_b = [
            ArticleImage(
                article_id=art_b.id, original_url="https://b/1.png",
                local_filename="1.png", local_path="/tmp/b1.png",
                description="d1", is_meaningful=True,
            ),
        ]
        s.add_all(images_a + images_b); await s.flush()

        s.add(ExtractionRun(source_id=src_b.id, status=RunStatus.RUNNING))

        await s.commit()
        return src_a.id, src_b.id


async def test_enrichment_stats(ctx, monkeypatch):
    c, factory = ctx
    a_id, b_id = await _seed_two_sources(factory)
    resp = await c.get("/api/dashboard/enrichment")
    assert resp.status_code == 200
    body = resp.json()
    rows = {r["source_id"]: r for r in body["sources"]}
    assert rows[str(a_id)]["described"] == 2 and rows[str(a_id)]["pending"] == 3  # decorative excluded
    assert rows[str(a_id)]["active_run"] is False
    assert rows[str(b_id)]["pending"] == 0 and rows[str(b_id)]["active_run"] is True
    assert body["aggregate"]["described"] == rows[str(a_id)]["described"] + rows[str(b_id)]["described"]
    assert body["aggregate"]["sources_with_backlog"] == 1  # only A has pending>0
