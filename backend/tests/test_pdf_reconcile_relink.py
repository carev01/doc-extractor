"""_reconcile_removals must relink PDF sections that share a #page URL to their
OWN TOC entry (by title tiebreak), not an arbitrary sibling. Regression for the
PDF-source-url-non-uniqueness weakness found during the 2026-07 dup investigation.
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
from app.models import Article, DocumentationSource, ExtractionRun, Product, Vendor
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
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_relinks_pdf_sections_by_title_on_shared_url(db):
    v = Vendor(name="V"); db.add(v); await db.flush()
    p = Product(vendor_id=v.id, name="P"); db.add(p); await db.flush()
    src = DocumentationSource(product_id=p.id, name="M", base_url="file://m.pdf", source_type="pdf")
    db.add(src); await db.flush()
    run = ExtractionRun(source_id=src.id); db.add(run); await db.flush()

    shared_url = "file://m.pdf#page=100"
    # Two outline sections starting on page 100 → same URL, different titles.
    toc_a = TOCEntry(source_id=src.id, title="Alpha", url=shared_url, level=1, sort_order=0)
    toc_b = TOCEntry(source_id=src.id, title="Beta", url=shared_url, level=1, sort_order=1)
    db.add_all([toc_a, toc_b]); await db.flush()

    # Two articles at that URL with NULL toc_entry_id (as after a TOC rebuild),
    # each identified by its title/topic_key.
    art_a = Article(source_id=src.id, title="Alpha", source_url=shared_url,
                    topic_key="alpha", content_markdown="a", content_hash="ha", toc_entry_id=None)
    art_b = Article(source_id=src.id, title="Beta", source_url=shared_url,
                    topic_key="beta", content_markdown="b", content_hash="hb", toc_entry_id=None)
    db.add_all([art_a, art_b]); await db.commit()

    await firecrawl_service._reconcile_removals(db, src.id, run.id)

    got_a = (await db.execute(select(Article).where(Article.id == art_a.id))).scalar_one()
    got_b = (await db.execute(select(Article).where(Article.id == art_b.id))).scalar_one()
    # Each relinks to its OWN (title-matching) TOC entry, and neither is removed.
    assert got_a.toc_entry_id == toc_a.id, "Alpha must relink to the Alpha TOC entry"
    assert got_b.toc_entry_id == toc_b.id, "Beta must relink to the Beta TOC entry"
    assert got_a.removed_at is None and got_b.removed_at is None
