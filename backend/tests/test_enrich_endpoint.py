"""Integration tests for POST /api/extraction/enrich/{source_id} (async httpx client)."""
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


async def _seed_source_with_undescribed_image(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        art = Article(
            source_id=src.id, title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown="# A", content_hash="h-a",
        )
        s.add(art); await s.flush()
        img = ArticleImage(
            article_id=art.id, original_url="https://x/a/img.png",
            local_filename="img.png", local_path="/tmp/img.png",
            description=None, is_meaningful=None,
        )
        s.add(img)
        await s.commit()
        return src.id


async def _seed_source_all_described(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        art = Article(
            source_id=src.id, title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown="# A", content_hash="h-a",
        )
        s.add(art); await s.flush()
        img = ArticleImage(
            article_id=art.id, original_url="https://x/a/img.png",
            local_filename="img.png", local_path="/tmp/img.png",
            description="x", is_meaningful=True,
        )
        s.add(img)
        await s.commit()
        return src.id


async def test_enrich_queues_run(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    c, factory = ctx
    src_id = await _seed_source_with_undescribed_image(factory)
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 200 and resp.json()["status"] == "pending"
    async with factory() as s:
        run = (await s.execute(select(ExtractionRun).where(ExtractionRun.source_id == src_id))).scalar_one()
        assert run.kind == "enrich" and run.status == RunStatus.PENDING


async def test_enrich_409_when_nothing_pending(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    c, factory = ctx
    src_id = await _seed_source_all_described(factory)   # every image has a description
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 409


async def test_enrich_409_when_disabled(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", False)
    c, factory = ctx
    src_id = await _seed_source_with_undescribed_image(factory)
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 409


async def test_enrich_409_when_active_run(ctx, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    c, factory = ctx
    src_id = await _seed_source_with_undescribed_image(factory)
    async with factory() as s:  # a pre-existing active run
        s.add(ExtractionRun(source_id=src_id, status=RunStatus.RUNNING)); await s.commit()
    resp = await c.post(f"/api/extraction/enrich/{src_id}")
    assert resp.status_code == 409
