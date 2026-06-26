import os
import sys
import uuid

import fitz
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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.extraction_run import RunStatus
from app.services.firecrawl import FirecrawlService
from app.services.pdf_import import pdf_path_for

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


async def test_extract_source_runs_pdf_pipeline(factory, tmp_path):
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.commit()
        sid, rid = src.id, run.id
    doc = fitz.open(); doc.new_page().insert_text((72, 72), "Hello")
    doc.set_toc([[1, "Intro", 1]])
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(doc.tobytes())

    svc = FirecrawlService()
    async with factory() as s:
        await svc.extract_source(s, sid, run_id=rid)
        await s.commit()

    async with factory() as s:
        run = await s.get(ExtractionRun, rid)
        assert run.status == RunStatus.COMPLETED
        n = (await s.execute(select(Article).where(Article.source_id == sid))).scalars().all()
        assert len(n) == 1 and n[0].title == "Intro"


async def test_corrupt_pdf_marks_run_failed_not_orphaned(factory, tmp_path):
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.commit()
        sid, rid = src.id, run.id
    # A file with PDF content-type bytes that PyMuPDF cannot parse.
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(b"%PDF-1.4 not actually a valid pdf body")

    svc = FirecrawlService()
    async with factory() as s:
        await svc.extract_source(s, sid, run_id=rid)
        await s.commit()

    # The run must be FAILED (with a message) — never left orphaned in RUNNING.
    async with factory() as s:
        run = await s.get(ExtractionRun, rid)
        assert run.status == RunStatus.FAILED
        assert run.error_message
        src = await s.get(DocumentationSource, sid)
        assert src.status.value == "failed"
