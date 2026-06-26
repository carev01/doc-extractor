import os
import sys
import uuid

import fitz
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    Vendor, Product, DocumentationSource, ExtractionRun, Article,
)
from app.models.article_version import ArticleVersion
from app.services.firecrawl import FirecrawlService
from app.services.pdf_import import run_pdf_extraction, pdf_path_for

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


def _pdf(extra="") -> bytes:
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Body for chapter {i+1}. {extra}")
    doc.set_toc([[1, "Chapter 1", 1], [1, "Chapter 2", 2]])
    return doc.tobytes()


async def _make_pdf_source(factory, tmp_path) -> uuid.UUID:
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="Manual",
            base_url="file://x.pdf", source_type="pdf",
        )
        s.add(src); await s.commit()
        sid = src.id
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf())
    return sid


async def _run(factory, sid) -> uuid.UUID:
    svc = FirecrawlService()
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        run = ExtractionRun(source_id=sid)
        s.add(run); await s.flush()
        run_pk = run.id
        await run_pdf_extraction(svc, s, src, run, run_pk)
        await s.commit()
    return run_pk


async def test_first_run_creates_articles(factory, tmp_path):
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    async with factory() as s:
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid).order_by(Article.sort_order)
        )).scalars().all()
        assert [a.title for a in arts] == ["Chapter 1", "Chapter 2"]
        assert all(a.content_markdown.strip() for a in arts)


async def test_second_identical_run_is_all_unchanged(factory, tmp_path):
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    run2 = await _run(factory, sid)
    async with factory() as s:
        r = await s.get(ExtractionRun, run2)
        assert r.articles_unchanged == 2
        assert r.articles_extracted == 0
        assert r.pdf_hash is not None


async def test_modified_pdf_diffs(factory, tmp_path):
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    # Replace the stored file with modified content, then re-run.
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf(extra="CHANGED"))
    await _run(factory, sid)
    async with factory() as s:
        nver = (await s.execute(
            select(func.count()).select_from(ArticleVersion)
            .join(Article, Article.id == ArticleVersion.article_id)
            .where(Article.source_id == sid)
        )).scalar()
        assert nver >= 1  # at least one prior version snapshotted
