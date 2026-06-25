"""Integration: a bot-block page must not be stored as an article.

Runs against docextractor_test (async). Verifies process_article_result rejects
an Akamai-style block page, records the condition on the run, and stores nothing
— the data-integrity guarantee that a WAF challenge never masquerades as content.
"""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, Article, ExtractionRun
from app.services.firecrawl import firecrawl_service, _BLOCKED_MSG

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

AKAMAI = (
    "Access Denied\n\nYou don't have permission to access this server.\n\n"
    "Reference #18.abc\n\nhttps://errors.edgesuite.net/18.abc\n"
)


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


async def _source_and_run(f):
    async with f() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="PPDM"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Guide",
                                  base_url="https://www.dell.com/support/manuals/en-us/x/y")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.commit()
        return src.id, run.id


async def test_block_page_not_stored_and_run_flagged(factory):
    src_id, run_id = await _source_and_run(factory)
    async with factory() as db:
        result = await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id,
            url="https://www.dell.com/support/manuals/en-us/x/y/topic?guid=guid-1&lang=en-us",
            markdown_content=AKAMAI, doc_html="<html>Access Denied</html>",
            toc_entry_id=None, sort_order=0, title="Topic",
        )
    assert result == "blocked"
    async with factory() as db:
        arts = (await db.execute(select(Article))).scalars().all()
        assert arts == []                       # nothing stored
        run = await db.get(ExtractionRun, run_id)
        assert run.error_message == _BLOCKED_MSG  # condition recorded on the run


async def test_real_content_still_stored(factory):
    src_id, run_id = await _source_and_run(factory)
    async with factory() as db:
        result = await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id,
            url="https://www.dell.com/support/manuals/en-us/x/y/topic?guid=guid-2&lang=en-us",
            markdown_content="# Getting started\n\nReal documentation body here.",
            doc_html="<h1>Getting started</h1>",
            toc_entry_id=None, sort_order=0, title="Getting started",
        )
    assert result in ("new", "updated")
    async with factory() as db:
        arts = (await db.execute(select(Article))).scalars().all()
        assert len(arts) == 1
