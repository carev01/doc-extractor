"""kind="escalate" retry: reuse the cached converted doc, re-escalate the pending
pages, re-split, and re-persist — so recovered content becomes real (sub-)articles
and reaches the delta feed. Fully monkeypatched (acquire/outline/page-texts/cache/
VLM) so the test needs neither the PDF store nor docling.
"""
import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.content_change import ContentChange
from app.models.extraction_run import RunStatus
from app.services.firecrawl import FirecrawlService
from app.services.pdf_convert import ConvertedDoc, rebuild_from_pages
import app.services.pdf_escalate as pdf_escalate
import app.services.pdf_import as pdf_import

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

# Outline (PDF bookmarks): a chapter and a sub-section the standard pipeline lost.
_OUTLINE = [
    pdf_import.Segment(title="Cloud", level=1, page_start=0, page_end=1, path=["Cloud"]),
    pdf_import.Segment(title="Azure", level=2, page_start=1, page_end=1, path=["Cloud", "Azure"]),
]


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    await engine.dispose()


async def _seed(f):
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id, source_type="pdf")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, kind="escalate", status=RunStatus.RUNNING,
                            escalation_pending=[{"page_start": 1, "page_end": 1}])
        s.add(run); await s.flush()
        await s.commit()
        return src.id, run.id


def _patch(monkeypatch, tmp_path, *, page1_result):
    """Wire the escalate-retry to a cached 2-page doc (page 1 empty = Azure lost)."""
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "pdf_vlm_escalation_enabled", True)

    async def fake_acquire(source, auth_cookies=None):
        return (b"%PDF fake", "hash1")

    md, starts = rebuild_from_pages(["# Cloud\n\nOverview of cloud platforms.", ""])

    def fake_load(pdf_hash, page_texts):
        return ConvertedDoc(markdown=md, headings=[], page_texts=page_texts,
                            table_pages=set(), images=[], engine="docling",
                            page_line_starts=starts)

    async def fake_page(pdf_bytes, p0):
        return page1_result  # None (still failing) or (markdown, images)

    monkeypatch.setattr(pdf_import, "acquire_pdf", fake_acquire)
    monkeypatch.setattr(pdf_import, "_outline_for", lambda b: _OUTLINE)
    monkeypatch.setattr(pdf_import, "_page_texts", lambda b: ["Overview of cloud platforms.", ""])
    monkeypatch.setattr(pdf_import.pdf_cache, "load", fake_load)
    monkeypatch.setattr(pdf_escalate, "escalate_page", fake_page)


async def _run(f, src_id, run_id):
    svc = FirecrawlService()
    async with f() as db:
        source = await db.get(DocumentationSource, src_id)
        run = await db.get(ExtractionRun, run_id)
        await pdf_import.retry_escalation(svc, db, source, run, run_id)
        await db.commit()


async def test_retry_recovers_subarticle_clears_pending_and_emits_changes(factory, tmp_path, monkeypatch):
    src_id, run_id = await _seed(factory)
    _patch(monkeypatch, tmp_path,
           page1_result=("## Azure\n\nProtect Azure workloads with the connector.", []))

    await _run(factory, src_id, run_id)

    async with factory() as s:
        arts = (await s.execute(
            select(Article).where(Article.source_id == src_id, Article.removed_at.is_(None))
        )).scalars().all()
        titles = {a.title for a in arts}
        assert "Azure" in titles                       # recovered as its own article
        azure = next(a for a in arts if a.title == "Azure")
        assert "Protect Azure workloads" in azure.content_markdown

        run = await s.get(ExtractionRun, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.escalation_pending is None          # warning cleared

        changes = (await s.execute(
            select(ContentChange).where(ContentChange.run_id == run_id)
        )).scalars().all()
        types = [c.change_type for c in changes]
        assert "run_start" in types                    # committed floor (delta gap-freeness)
        assert any(t != "run_start" for t in types)    # content reached the outbox


async def test_retry_keeps_pending_when_still_failing(factory, tmp_path, monkeypatch):
    src_id, run_id = await _seed(factory)
    _patch(monkeypatch, tmp_path, page1_result=None)   # VLM still down

    await _run(factory, src_id, run_id)

    async with factory() as s:
        run = await s.get(ExtractionRun, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.escalation_pending == [{"page_start": 1, "page_end": 1}]  # still pending
