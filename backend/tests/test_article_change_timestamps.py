"""Change timestamps: when the CONTENT changed, as distinct from when we crawled.

A downstream temporal knowledge graph orders facts by these and decides which of
two conflicting facts is current by comparing them. Before this existed it fell
back to the crawl time, and since a bulk run stamps thousands of articles within
the same minute (2,470 in one minute, measured), which fact survived a conflict
was decided by the order the crawler happened to visit pages.

So the property under test throughout is: a re-crawl that returns identical bytes
must NOT move these, while extracted_at does.
"""

import os
import sys

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
from app.models.extraction_run import RunStatus
from app.services import change_log
from app.services.firecrawl import firecrawl_service

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

URL = "https://docs.example.com/a"


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


async def _source(f):
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="S",
                                  base_url="https://docs.example.com")
        s.add(src); await s.flush()
        await s.commit()
        return src.id


async def _run(f, source_id):
    # Created already-terminal: uq_active_run_per_source allows only one ACTIVE
    # run per source, and these tests only need a run row to attribute changes
    # to, not a live one.
    async with f() as s:
        run = ExtractionRun(source_id=source_id, status=RunStatus.COMPLETED)
        s.add(run); await s.commit()
        return run.id


async def _scrape(f, source_id, markdown, *, title="T"):
    """One pass of the real content path, as an extraction run would do it."""
    run_id = await _run(f, source_id)
    async with f() as db:
        outcome = await firecrawl_service.process_article_result(
            db=db, source_id=source_id, run_id=run_id, url=URL,
            markdown_content=markdown, doc_html=f"<article>{markdown}</article>",
            toc_entry_id=None, sort_order=0, title=title,
        )
        await db.commit()
    return outcome, run_id


async def _article(f, source_id) -> Article:
    async with f() as s:
        return (await s.execute(
            select(Article).where(Article.source_id == source_id)
        )).scalars().one()


async def test_first_capture_dates_both_axes(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id = await _source(factory)

    outcome, _ = await _scrape(factory, src_id, "# Title\n\nBody one.")
    assert outcome == "new"

    a = await _article(factory, src_id)
    assert a.content_changed_at is not None
    assert a.source_changed_at is not None
    assert a.content_changed_basis == change_log.BASIS_EXACT


async def test_unchanged_recrawl_does_not_move_the_timestamps(factory, tmp_path, monkeypatch):
    """The whole point of the field. extracted_at advances; these do not."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id = await _source(factory)
    await _scrape(factory, src_id, "# Title\n\nStable body.")

    before = await _article(factory, src_id)
    first_content = before.content_changed_at
    first_source = before.source_changed_at
    first_extracted = before.extracted_at

    outcome, _ = await _scrape(factory, src_id, "# Title\n\nStable body.")
    assert outcome == "unchanged"

    after = await _article(factory, src_id)
    assert after.content_changed_at == first_content
    assert after.source_changed_at == first_source
    # ...while the crawl timestamp did move, which is what made it unusable as a
    # content-change signal.
    assert after.extracted_at > first_extracted


async def test_changed_content_moves_both(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id = await _source(factory)
    await _scrape(factory, src_id, "# Title\n\nFirst body.")
    before = await _article(factory, src_id)

    outcome, _ = await _scrape(factory, src_id, "# Title\n\nSecond body, rewritten.")
    assert outcome == "updated"

    after = await _article(factory, src_id)
    assert after.content_changed_at > before.content_changed_at
    assert after.source_changed_at > before.source_changed_at
    assert after.content_changed_basis == change_log.BASIS_EXACT


async def test_enrichment_moves_content_but_not_source(factory, tmp_path, monkeypatch):
    """The distinction the two fields exist to draw.

    Injecting an image caption changes the markdown we serve — and the delta
    feed's content_hash, which is taken over the served text — but the vendor's
    page did not change. A consumer orders facts by content_changed_at (so a
    re-ingest can always be dated) and gates "did the vendor actually change
    anything" on source_changed_at.
    """
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id = await _source(factory)
    await _scrape(factory, src_id, "# Title\n\nBody with an image.")

    before = await _article(factory, src_id)
    run_id = await _run(factory, src_id)

    # What enrich_run_images does: edit content_markdown, then record the change.
    async with factory() as db:
        article = (await db.execute(
            select(Article).where(Article.source_id == src_id)
        )).scalars().one()
        article.content_markdown += "\n\n> **Figure:** A screenshot of the console."
        await change_log.record_change(
            db, article=article, change_type="updated", run_id=run_id)
        await db.commit()

    after = await _article(factory, src_id)
    assert after.content_changed_at > before.content_changed_at, \
        "served markdown changed, so the content timestamp must move"
    assert after.source_changed_at == before.source_changed_at, \
        "the vendor's page did not change, so the source timestamp must not move"


async def test_source_changed_at_survives_an_enrichment_only_history(factory, tmp_path, monkeypatch):
    """source_changed_at keeps pointing at the last REAL vendor change even after
    several enrichment passes — otherwise it could not answer the question it
    exists for."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id = await _source(factory)
    await _scrape(factory, src_id, "# Title\n\nOriginal.")
    await _scrape(factory, src_id, "# Title\n\nVendor rewrote this.")
    vendor_change = (await _article(factory, src_id)).source_changed_at

    for n in range(2):
        run_id = await _run(factory, src_id)
        async with factory() as db:
            article = (await db.execute(
                select(Article).where(Article.source_id == src_id)
            )).scalars().one()
            article.content_markdown += f"\n\n> **Figure:** Caption {n}."
            await change_log.record_change(
                db, article=article, change_type="updated", run_id=run_id)
            await db.commit()

    after = await _article(factory, src_id)
    assert after.source_changed_at == vendor_change
    assert after.content_changed_at > vendor_change
