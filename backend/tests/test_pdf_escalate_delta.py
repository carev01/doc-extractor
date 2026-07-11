"""retry_escalation must surface VLM-improved PDF content in the delta feed.

The kind="escalate" retry re-converts low-confidence PDF segments and updates
article content in place. Before this fix it wrote an ArticleVersion snapshot but
no content_changes outbox row, so VLM-improved content never reached the delta
feed. It must now (a) commit a run_start sentinel floor before mutating (parity
with extract_source, so concurrent-run gap-freeness holds) and (b) emit an
`updated` content_changes row for each article whose content actually changes.

Async harness against docextractor_test; acquire_pdf and escalate_segment are
monkeypatched so the test doesn't depend on the PDF store or docling/VLM.
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
from app.models.content_change import ContentChange
from app.models.extraction_run import RunStatus
from app.services.firecrawl import FirecrawlService, compute_content_hash
import app.services.pdf_import as pdf_import

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
    await engine.dispose()


async def _seed(f, *, initial_md="# Old\n\nOld content."):
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id, source_type="pdf")
        s.add(src); await s.flush()
        art = Article(
            source_id=src.id, title="Sec", source_url="https://x#sec", topic_key="https://x#sec",
            content_markdown=initial_md, content_hash=compute_content_hash(initial_md),
        )
        s.add(art); await s.flush()
        run = ExtractionRun(source_id=src.id, kind="escalate", status=RunStatus.RUNNING)
        run.escalation_pending = [{
            "article_id": str(art.id), "title": "Sec", "level": 1,
            "page_start": 0, "page_end": 1,
        }]
        s.add(run); await s.flush()
        await s.commit()
        return src.id, art.id, run.id


def _patch(monkeypatch, *, new_md):
    async def fake_acquire(source, auth_cookies=None):
        return (b"%PDF-1.4 fake", "x.pdf")

    async def fake_escalate(pdf_bytes, seg):
        return new_md

    monkeypatch.setattr(pdf_import, "acquire_pdf", fake_acquire)
    monkeypatch.setattr(pdf_import, "escalate_segment", fake_escalate)


async def _run(f, src_id, run_id):
    svc = FirecrawlService()
    async with f() as db:
        source = await db.get(DocumentationSource, src_id)
        run = await db.get(ExtractionRun, run_id)
        await pdf_import.retry_escalation(svc, db, source, run, run_id)
        await db.commit()


async def test_escalation_content_change_emits_updated_outbox_row(factory, monkeypatch):
    src_id, art_id, run_id = await _seed(factory)
    new_md = "# New\n\nVLM-improved, materially different content."
    _patch(monkeypatch, new_md=new_md)

    await _run(factory, src_id, run_id)

    async with factory() as s:
        changes = (await s.execute(
            select(ContentChange).where(ContentChange.run_id == run_id).order_by(ContentChange.id)
        )).scalars().all()
        types = [c.change_type for c in changes]
        # A run_start floor, then an `updated` row for the changed article.
        assert "run_start" in types
        updated = [c for c in changes if c.change_type == "updated"]
        assert len(updated) == 1
        assert updated[0].article_id == art_id
        assert updated[0].content_hash == compute_content_hash(new_md)

        art = await s.get(Article, art_id)
        assert art.content_markdown == new_md  # content actually applied


async def test_escalation_no_content_change_emits_no_updated_row(factory, monkeypatch):
    # escalate returns byte-identical content → no ArticleVersion, no `updated`
    # outbox row (but the run_start floor is still committed).
    same_md = "# Old\n\nOld content."
    src_id, art_id, run_id = await _seed(factory, initial_md=same_md)
    _patch(monkeypatch, new_md=same_md)

    await _run(factory, src_id, run_id)

    async with factory() as s:
        changes = (await s.execute(
            select(ContentChange).where(ContentChange.run_id == run_id)
        )).scalars().all()
        types = [c.change_type for c in changes]
        assert "run_start" in types
        assert "updated" not in types
