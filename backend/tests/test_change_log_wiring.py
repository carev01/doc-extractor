"""The extraction persistence sites emit content_changes rows.

Async httpx-style harness (async session against docextractor_test), because
process_article_result and _reconcile_removals are async and use db.commit().
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.toc import TOCEntry
from app.models.content_change import ContentChange
from app.services.firecrawl import FirecrawlService

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


async def _seed(s):
    vendor = Vendor(name="V"); s.add(vendor); await s.flush()
    product = Product(name="P", vendor_id=vendor.id); s.add(product); await s.flush()
    source = DocumentationSource(name="S", base_url="https://x", product_id=product.id)
    s.add(source); await s.flush()
    run = ExtractionRun(source_id=source.id); s.add(run); await s.flush()
    await s.commit()
    return source, run


async def test_new_article_emits_added_change(session):
    source, run = await _seed(session)
    svc = FirecrawlService()
    outcome = await svc.process_article_result(
        session, source_id=source.id, run_id=run.id,
        url="https://x/a", markdown_content="# A\n\nBody text here.",
        doc_html="", toc_entry_id=None, sort_order=0, title="A",
    )
    assert outcome == "new"
    rows = (await session.execute(select(ContentChange))).scalars().all()
    assert len(rows) == 1
    assert rows[0].change_type == "added"
    assert rows[0].run_id == run.id


async def test_changed_article_emits_updated_change(session):
    source, run = await _seed(session)
    svc = FirecrawlService()
    await svc.process_article_result(
        session, source_id=source.id, run_id=run.id, url="https://x/a",
        markdown_content="# A\n\nfirst.", doc_html="", toc_entry_id=None,
        sort_order=0, title="A",
    )
    await svc.process_article_result(
        session, source_id=source.id, run_id=run.id, url="https://x/a",
        markdown_content="# A\n\nSECOND, changed.", doc_html="", toc_entry_id=None,
        sort_order=0, title="A",
    )
    types = [r.change_type for r in (await session.execute(
        select(ContentChange).order_by(ContentChange.id))).scalars().all()]
    assert types == ["added", "updated"]


async def test_reconcile_removals_emits_removed_change(session):
    source, run = await _seed(session)
    # An article whose TOC entry is gone (toc_entry_id NULL, url not in TOC) → removed.
    art = Article(
        source_id=source.id, extraction_run_id=run.id, created_run_id=run.id,
        title="Gone", source_url="https://x/gone", topic_key="https://x/gone",
        content_markdown="# Gone", content_hash="h", toc_entry_id=None,
    )
    session.add(art); await session.commit()
    svc = FirecrawlService()
    await svc._reconcile_removals(session, source.id, run.id)
    rows = (await session.execute(
        select(ContentChange).where(ContentChange.change_type == "removed"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].article_id == art.id
    assert rows[0].topic_key == "https://x/gone"
