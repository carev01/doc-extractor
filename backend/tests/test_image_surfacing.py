"""description/kind surface on GET /api/articles/{id} and the delta feed record."""
import json
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
from app.models.image import ArticleImage
from app.models.content_change import ContentChange

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


async def _seed_described(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="completed"); s.add(run); await s.flush()
        art = Article(source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                      title="A", source_url="https://x/a", topic_key="https://x/a",
                      content_markdown="# A\n\n![p](/media/x/y.png)\n\n> **Figure:** A diagram.\n",
                      content_hash="h")
        s.add(art); await s.flush()
        s.add(ArticleImage(article_id=art.id, original_url="u", local_filename="y.png",
                           local_path="/media/x/y.png", sort_order=0,
                           is_meaningful=True, description="A diagram.", kind="diagram",
                           width=400, height=300, bytes_sha256="a"*64))
        s.add(ContentChange(article_id=art.id, source_id=src.id, run_id=run.id,
                            change_type="added", content_hash="h", topic_key="https://x/a"))
        await s.commit()
        return art.id


async def test_article_detail_exposes_description(ctx):
    c, factory = ctx
    art_id = await _seed_described(factory)
    resp = await c.get(f"/api/articles/{art_id}")
    assert resp.status_code == 200
    img = resp.json()["images"][0]
    assert img["description"] == "A diagram." and img["kind"] == "diagram"
    assert img["width"] == 400 and img["height"] == 300


async def test_delta_record_includes_description(ctx):
    c, factory = ctx
    await _seed_described(factory)
    resp = await c.get("/api/articles/delta")  # bootstrap
    lines = [json.loads(l) for l in resp.text.splitlines() if l.strip()]
    rec = next(r for r in lines if r.get("change_type") == "added")
    assert rec["images"][0]["description"] == "A diagram."
    assert rec["images"][0]["kind"] == "diagram"
