import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.services.media_gc import gc_orphaned_media

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _article(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M", base_url="https://d")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.flush()
        art = Article(source_id=src.id, title="T", source_url="https://d/a",
                      topic_key="a", content_markdown="x")
        s.add(art); await s.commit()
        return art.id


def _mkdir(media_dir, name):
    d = os.path.join(media_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "f.png"), "wb") as fh:
        fh.write(b"x")
    return d


async def test_gc_removes_only_orphan_dirs(factory, tmp_path):
    media = str(tmp_path)
    live_id = await _article(factory)
    live = _mkdir(media, str(live_id))
    orphan = _mkdir(media, str(uuid.uuid4()))    # no such article
    nonuuid = _mkdir(media, "not-a-uuid")        # left untouched

    async with factory() as s:
        removed = await gc_orphaned_media(s, media)

    assert removed == 1
    assert os.path.isdir(live)
    assert not os.path.exists(orphan)
    assert os.path.isdir(nonuuid)


async def test_gc_handles_missing_media_dir(factory, tmp_path):
    async with factory() as s:
        removed = await gc_orphaned_media(s, str(tmp_path / "does-not-exist"))
    assert removed == 0
