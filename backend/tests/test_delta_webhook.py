"""The extraction_complete webhook payload carries a delta summary block."""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.content_change import ContentChange
from app.services import change_log
from app.schemas.delta import decode_delta_cursor

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_run_change_counts_feeds_delta_block(session):
    v = Vendor(name="V"); session.add(v); await session.flush()
    p = Product(name="P", vendor_id=v.id); session.add(p); await session.flush()
    src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
    session.add(src); await session.flush()
    run = ExtractionRun(source_id=src.id); session.add(run); await session.flush()
    for i in range(2):
        session.add(ContentChange(source_id=src.id, run_id=run.id, change_type="added", topic_key=f"t{i}"))
    session.add(ContentChange(source_id=src.id, run_id=run.id, change_type="updated", topic_key="u"))
    await session.commit()

    counts = await change_log.run_change_counts(session, run.id)
    assert counts == {"added": 2, "updated": 1, "removed": 0}
