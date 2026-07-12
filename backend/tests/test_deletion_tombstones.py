"""Deletion tombstones + append-only content_changes (async httpx client)."""
import os
import sys
import uuid

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
from app.models import Vendor, Product, DocumentationSource, Article
from app.models.content_change import ContentChange, ChangeType

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


async def _seed_article(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        art = Article(source_id=src.id, title="A", source_url="https://s/a",
                      topic_key="https://s/a", content_markdown="# A", content_hash="h")
        s.add(art); await s.flush()
        cc = ContentChange(article_id=art.id, source_id=src.id, run_id=None,
                           change_type=ChangeType.ADDED.value, content_hash="h",
                           topic_key="https://s/a")
        s.add(cc); await s.commit()
        return src.id, art.id, cc.id


async def test_hard_deleting_article_preserves_outbox_ids(ctx):
    """content_changes is append-only: deleting the article must NOT null its
    article_id on the historical outbox row."""
    _c, factory = ctx
    src_id, art_id, cc_id = await _seed_article(factory)
    async with factory() as s:
        art = (await s.execute(select(Article).where(Article.id == art_id))).scalar_one()
        await s.delete(art)
        await s.commit()
    async with factory() as s:
        cc = (await s.execute(select(ContentChange).where(ContentChange.id == cc_id))).scalar_one()
        assert cc.article_id == art_id      # was nulled by the SET NULL FK before this task
        assert cc.source_id == src_id
