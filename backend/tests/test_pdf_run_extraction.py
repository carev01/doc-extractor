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
from app.services.pdf_convert import ConvertedDoc
from app.services.pdf_import import run_pdf_extraction, pdf_path_for
import app.services.pdf_import as _pdf_import_mod

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


@pytest_asyncio.fixture(autouse=True)
async def patch_convert_pdf(monkeypatch):
    """Patch convert_pdf in the pdf_import namespace so integration tests don't
    depend on docling-serve availability. The fake generates ATX-heading markdown
    from the PDF's actual outline so split_into_segments can find section
    boundaries — the same logic the real pipeline uses, just without the network
    call."""
    async def fake_convert(pdf_bytes):
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            toc = doc.get_toc(simple=True)
            page_texts = [
                doc[i].get_text("text").strip()
                for i in range(doc.page_count)
            ]
        finally:
            doc.close()

        lines = []
        if toc:
            for level, title, page1 in toc:
                p = max(0, page1 - 1)
                text = page_texts[p] if p < len(page_texts) else ""
                lines.append(f"{'#' * level} {title}\n\n{text}")
        else:
            lines = [t for t in page_texts if t] or ["content"]

        md = "\n\n".join(lines)
        return ConvertedDoc(
            markdown=md, headings=[], page_texts=page_texts,
            table_pages=set(), images=[], engine="fake",
        )

    monkeypatch.setattr(_pdf_import_mod, "convert_pdf", fake_convert)


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


def _pdf_with_cover() -> bytes:
    """Same chapter content as _pdf(), but with a blank cover page inserted at
    the front — so each chapter's #page anchor shifts by one while its rendered
    markdown stays byte-identical."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Cover")          # page 0 (not in TOC)
    for i in range(2):
        doc.new_page().insert_text((72, 72), f"Body for chapter {i+1}. ")
    doc.set_toc([[1, "Chapter 1", 2], [1, "Chapter 2", 3]])
    return doc.tobytes()


async def test_page_shift_unchanged_section_not_removed(factory, tmp_path):
    """Inserting a cover page shifts every section's #page anchor (new pdf_hash,
    full re-run) but leaves each section's content byte-identical (hash match →
    'unchanged'). The unchanged articles must NOT be mis-flagged as removed —
    process_article_result advances source_url so _reconcile_removals re-links them."""
    sid = await _make_pdf_source(factory, tmp_path)
    await _run(factory, sid)
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf_with_cover())
    run2 = await _run(factory, sid)

    async with factory() as s:
        r = await s.get(ExtractionRun, run2)
        assert r.articles_unchanged == 2      # both sections matched by content hash
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid).order_by(Article.sort_order)
        )).scalars().all()
        assert [a.title for a in arts] == ["Chapter 1", "Chapter 2"]
        # The crux: neither section is flagged removed, and each points at its
        # new page-anchored URL.
        assert all(a.removed_at is None for a in arts)
        assert all(a.source_url.endswith(("#page=2", "#page=3")) for a in arts)


def _pdf_duplicate_titles() -> bytes:
    """Two top-level sections with the SAME title — their outline-path slugs
    collide unless disambiguated."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "First notes body.")
    doc.new_page().insert_text((72, 72), "Second notes body.")
    doc.set_toc([[1, "Notes", 1], [1, "Notes", 2]])
    return doc.tobytes()


async def test_duplicate_sibling_titles_do_not_collide(factory, tmp_path):
    """Two sibling sections sharing a title must each get their own article — the
    second must not overwrite the first via a colliding topic_key."""
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Manual",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.commit()
        sid = src.id
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf_duplicate_titles())
    await _run(factory, sid)

    async with factory() as s:
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid).order_by(Article.sort_order)
        )).scalars().all()
        assert len(arts) == 2                                  # neither clobbered
        keys = sorted(a.topic_key for a in arts)
        assert keys == ["notes", "notes-2"]                    # disambiguated
        bodies = " ".join(a.content_markdown for a in arts)
        assert "First notes body." in bodies and "Second notes body." in bodies


async def test_articles_total_excludes_empty_segments(factory, tmp_path, monkeypatch):
    """A segment that renders to empty markdown is not persisted, so it must not
    count toward articles_total — otherwise progress can never reach 100%."""
    from app.services.pdf_convert import RenderedSegment

    sid = await _make_pdf_source(factory, tmp_path)  # _pdf() → 2 sections

    # Force the second section to render empty (e.g. an image-only page).
    async def _fake_build(pdf_bytes, progress=None):
        segs = [
            RenderedSegment(title="Chapter 1", level=1, path=["Chapter 1"],
                            page_start=0, page_end=0, markdown="Real content.", images=[]),
            RenderedSegment(title="Chapter 2", level=1, path=["Chapter 2"],
                            page_start=1, page_end=1, markdown="", images=[]),
        ]
        if progress is not None:
            for i in range(len(segs)):
                await progress(i + 1, len(segs))
        return segs

    monkeypatch.setattr(_pdf_import_mod, "build_segments", _fake_build)

    run_pk = await _run(factory, sid)
    async with factory() as s:
        r = await s.get(ExtractionRun, run_pk)
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalars().all()
        assert len(arts) == 1                       # empty segment not persisted
        processed = r.articles_extracted + r.articles_updated + r.articles_unchanged
        assert r.articles_total == processed == 1   # denominator matches reality
