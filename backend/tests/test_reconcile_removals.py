"""Regression tests for FirecrawlService._reconcile_removals.

Phase 1 deletes the old TOC, which NULLs every article's toc_entry_id via the
ON DELETE SET NULL FK. Phase 2 re-links each page as it is scraped — but a
*resumed* run skips pages already scraped in a prior cycle, so without an
authoritative re-link those articles would stay NULL and be wrongly flagged
removed. These tests pin that behaviour.

Uses the async asyncpg harness (like test_queue) against docextractor_test.
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
from app.models import Vendor, Product, DocumentationSource, Article, ExtractionRun
from app.models.extraction_run import RunStatus
from app.models.toc import TOCEntry
from app.services.firecrawl import firecrawl_service

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(db) -> uuid.UUID:
    v = Vendor(name=f"V-{uuid.uuid4().hex[:8]}")
    db.add(v)
    await db.flush()
    s_prod = Product(vendor_id=v.id, name="P")
    db.add(s_prod)
    await db.flush()
    s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
    db.add(s)
    await db.flush()
    return s.id


async def _run(db, source_id) -> uuid.UUID:
    r = ExtractionRun(source_id=source_id, status=RunStatus.RUNNING,
                      articles_total=0, articles_extracted=0)
    db.add(r)
    await db.flush()
    return r.id


def _toc(source_id, url, order):
    return TOCEntry(source_id=source_id, title=url, url=url, level=0,
                    sort_order=order, is_article=True, parent_id=None)


def _article(source_id, url, toc_entry_id=None):
    return Article(
        source_id=source_id, title=url, source_url=url, topic_key=url,
        content_markdown="x", content_hash="h", sort_order=0,
        estimated_tokens=1, content_size_bytes=1, toc_entry_id=toc_entry_id,
    )


@pytest.mark.asyncio
async def test_resumed_articles_with_null_link_are_not_removed(db):
    """The resume bug: article URL still in the TOC but toc_entry_id is NULL
    (never re-linked because it was skipped). It must be re-linked, not removed."""
    source_id = await _source(db)
    run_id = await _run(db, source_id)
    # Current TOC has the page...
    te = _toc(source_id, "http://x/a", 0)
    db.add(te)
    # ...but the article's link was NULLed by the Phase-1 TOC delete and never
    # restored (it was skipped on resume).
    db.add(_article(source_id, "http://x/a", toc_entry_id=None))
    await db.commit()

    await firecrawl_service._reconcile_removals(db, source_id, run_id)

    art = (await db.execute(select(Article))).scalar_one()
    assert art.removed_at is None, "page still in TOC must not be flagged removed"
    assert art.toc_entry_id == te.id, "article must be re-linked to current TOC entry"


@pytest.mark.asyncio
async def test_article_absent_from_toc_is_removed(db):
    """A page genuinely gone from the rebuilt TOC is stamped removed."""
    source_id = await _source(db)
    run_id = await _run(db, source_id)
    db.add(_toc(source_id, "http://x/keep", 0))
    db.add(_article(source_id, "http://x/keep", toc_entry_id=None))
    db.add(_article(source_id, "http://x/gone", toc_entry_id=None))
    await db.commit()

    await firecrawl_service._reconcile_removals(db, source_id, run_id)

    rows = {a.source_url: a for a in (await db.execute(select(Article))).scalars()}
    assert rows["http://x/keep"].removed_at is None
    assert rows["http://x/gone"].removed_at is not None
    assert rows["http://x/gone"].removal_run_id == run_id


@pytest.mark.asyncio
async def test_returned_page_clears_removal_flag(db):
    """A previously-removed page that reappears in the TOC has its flag cleared."""
    from datetime import datetime, timezone
    source_id = await _source(db)
    run_id = await _run(db, source_id)
    te = _toc(source_id, "http://x/back", 0)
    db.add(te)
    a = _article(source_id, "http://x/back", toc_entry_id=None)
    a.removed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    a.removal_run_id = run_id
    db.add(a)
    await db.commit()

    await firecrawl_service._reconcile_removals(db, source_id, run_id)

    art = (await db.execute(select(Article))).scalar_one()
    assert art.removed_at is None
    assert art.removal_run_id is None
    assert art.toc_entry_id == te.id


@pytest.mark.asyncio
async def test_same_url_articles_keep_distinct_toc_links(db):
    """PDF sections sharing a #page URL, each already linked to its OWN TOC entry,
    must NOT be collapsed onto one entry by the URL re-link."""
    source_id = await _source(db)
    run_id = await _run(db, source_id)
    te1 = _toc(source_id, "http://x/doc#page=6", 0)
    te2 = _toc(source_id, "http://x/doc#page=6", 1)  # same url, different entry
    db.add_all([te1, te2])
    await db.flush()
    a1 = _article(source_id, "http://x/doc#page=6", toc_entry_id=te1.id)
    a1.topic_key = "k1"
    a2 = _article(source_id, "http://x/doc#page=6", toc_entry_id=te2.id)
    a2.topic_key = "k2"
    db.add_all([a1, a2])
    await db.commit()

    await firecrawl_service._reconcile_removals(db, source_id, run_id)

    rows = {a.topic_key: a for a in (await db.execute(select(Article))).scalars()}
    assert rows["k1"].toc_entry_id == te1.id
    assert rows["k2"].toc_entry_id == te2.id
    assert rows["k1"].removed_at is None and rows["k2"].removed_at is None
