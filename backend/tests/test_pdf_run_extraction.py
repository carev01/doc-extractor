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
from app.models.extraction_run import RunStatus
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
    async def fake_convert(pdf_bytes, on_poll=None, on_progress=None):
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


def _same_page_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    # Two outline sections that both start on page 1 (like Avamar's Data server /
    # MCS / EMT, all on page 25).
    doc.set_toc([[1, "Data server", 1], [1, "MCS", 1]])
    return doc.tobytes()


async def test_same_page_sections_each_persist_their_own_article(factory, tmp_path, monkeypatch):
    # Regression: two sections beginning on the SAME page must each get their own
    # article. They previously collapsed into one because they shared a "#page=N"
    # source_url and the URL-healing fallback (processing them in turn) kept
    # overwriting the single running survivor at that URL.
    sid = await _make_pdf_source(factory, tmp_path)
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_same_page_pdf())
    md = "## Data server\n\ndata server body\n\n## MCS\n\nmcs body\n"

    async def fake_convert(pdf_bytes, on_poll=None, on_progress=None):
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md],
                            table_pages=set(), images=[], engine="docling",
                            page_line_starts=[0])

    monkeypatch.setattr(_pdf_import_mod, "convert_pdf", fake_convert)
    monkeypatch.setattr(_pdf_import_mod.settings, "pdf_vlm_escalation_enabled", False)
    await _run(factory, sid)
    async with factory() as s:
        arts = (await s.execute(
            select(Article)
            .where(Article.source_id == sid, Article.removed_at.is_(None))
            .order_by(Article.sort_order)
        )).scalars().all()
        assert [a.title for a in arts] == ["Data server", "MCS"]
        assert "data server body" in arts[0].content_markdown
        assert "mcs body" in arts[1].content_markdown
        assert "data server body" not in arts[1].content_markdown
        # Distinct identities despite the shared page anchor.
        assert arts[0].source_url != arts[1].source_url


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
    def _fake_split(converted, outline):
        return [
            RenderedSegment(title="Chapter 1", level=1, path=["Chapter 1"],
                            page_start=0, page_end=0, markdown="Real content.", images=[]),
            RenderedSegment(title="Chapter 2", level=1, path=["Chapter 2"],
                            page_start=1, page_end=1, markdown="", images=[]),
        ]

    monkeypatch.setattr(_pdf_import_mod, "split_into_segments", _fake_split)

    run_pk = await _run(factory, sid)
    async with factory() as s:
        r = await s.get(ExtractionRun, run_pk)
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalars().all()
        assert len(arts) == 1                       # empty segment not persisted
        processed = r.articles_extracted + r.articles_updated + r.articles_unchanged
        assert r.articles_total == processed == 1   # denominator matches reality


async def test_relocated_url_preserves_history_and_records_version_origin(factory, tmp_path):
    """Changing a from-URL PDF's URL (relocation) must keep tracking the same
    articles — identity is the heading-path topic_key, not the URL — and each
    superseded version must retain the URL it was captured at, so links to
    previous versions survive the move."""
    sid = await _make_pdf_source(factory, tmp_path)  # base_url file://x.pdf
    await _run(factory, sid)

    # Relocate the source URL AND change the content so a version is snapshotted.
    # Use another file:// URL so acquire_pdf reads the locally-stored blob (by
    # source id, not base_url) — keeping the test offline. The http(s)://
    # relocation path (and its guards) is covered in test_pdf_source_api.
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        src.base_url = "file://relocated.pdf"
        await s.commit()
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf(extra="CHANGED"))
    await _run(factory, sid)

    async with factory() as s:
        arts = (await s.execute(
            select(Article).where(Article.source_id == sid).order_by(Article.sort_order)
        )).scalars().all()
        # Same two articles (matched by topic_key), not duplicated or removed.
        assert [a.title for a in arts] == ["Chapter 1", "Chapter 2"]
        assert all(a.removed_at is None for a in arts)
        # Live articles now point at the relocated URL.
        assert all(a.source_url.startswith("file://relocated.pdf") for a in arts)
        # Each prior version kept its ORIGINAL (pre-relocation) source URL.
        vers = (await s.execute(
            select(ArticleVersion)
            .join(Article, Article.id == ArticleVersion.article_id)
            .where(Article.source_id == sid)
        )).scalars().all()
        assert vers
        assert all(v.source_url.startswith("file://x.pdf") for v in vers)


async def test_escalation_failure_records_pending(factory, tmp_path, monkeypatch):
    """When escalation reports failed segments, the run completes but records them
    in escalation_pending (→ warning + retry-eligible), mapped to their article."""
    sid = await _make_pdf_source(factory, tmp_path)

    async def fake_escalate(pdf_bytes, segments, converted, on_event=None):
        return [0]  # the first segment (Chapter 1) failed escalation

    monkeypatch.setattr(_pdf_import_mod, "escalate_segments", fake_escalate)
    run_pk = await _run(factory, sid)

    async with factory() as s:
        run = await s.get(ExtractionRun, run_pk)
        assert run.status == RunStatus.COMPLETED        # still completes
        assert run.escalation_pending and len(run.escalation_pending) == 1
        entry = run.escalation_pending[0]
        assert entry["page_start"] == 0 and entry["title"] == "Chapter 1"
        art = await s.get(Article, uuid.UUID(entry["article_id"]))
        assert art.title == "Chapter 1"


async def test_retry_escalation_updates_articles_and_clears_pending(factory, tmp_path, monkeypatch):
    """A kind='escalate' retry re-converts only the pending pages, updates the
    affected article (snapshotting the prior content), and clears the warning."""
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.flush()
        art = Article(
            source_id=src.id, title="Chapter 1", source_url="file://x.pdf#page=1",
            topic_key="chapter-1", content_markdown="OLD", content_hash="h0", sort_order=0,
        )
        s.add(art); await s.flush()
        run = ExtractionRun(
            source_id=src.id, kind="escalate", status=RunStatus.RUNNING,
            escalation_pending=[{
                "article_id": str(art.id), "page_start": 0, "page_end": 0,
                "level": 1, "title": "Chapter 1",
            }],
        )
        s.add(run); await s.commit()
        sid, aid, rid = src.id, art.id, run.id
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf())

    async def good(pdf_bytes, segment):
        return "## Chapter 1\n\nIMPROVED TABLE"

    monkeypatch.setattr(_pdf_import_mod, "escalate_segment", good)
    svc = FirecrawlService()
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        run = await s.get(ExtractionRun, rid)
        await _pdf_import_mod.retry_escalation(svc, s, src, run, rid)
        await s.commit()

    async with factory() as s:
        art = await s.get(Article, aid)
        assert "IMPROVED TABLE" in art.content_markdown
        run = await s.get(ExtractionRun, rid)
        assert run.status == RunStatus.COMPLETED
        assert run.escalation_pending is None        # warning cleared
        assert run.articles_updated == 1
        ver = (await s.execute(
            select(ArticleVersion).where(ArticleVersion.article_id == aid)
        )).scalar_one()
        assert ver.content_markdown == "OLD"          # prior content snapshotted
        assert ver.source_url == "file://x.pdf#page=1"


async def test_retry_escalation_keeps_pending_when_still_failing(factory, tmp_path, monkeypatch):
    """If the VLM is still down, the retry leaves the segment pending so the run
    keeps its warning and can be retried again."""
    settings.pdf_dir = str(tmp_path)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.flush()
        art = Article(
            source_id=src.id, title="Chapter 1", source_url="file://x.pdf#page=1",
            topic_key="chapter-1", content_markdown="OLD", content_hash="h0", sort_order=0,
        )
        s.add(art); await s.flush()
        run = ExtractionRun(
            source_id=src.id, kind="escalate", status=RunStatus.RUNNING,
            escalation_pending=[{
                "article_id": str(art.id), "page_start": 0, "page_end": 0,
                "level": 1, "title": "Chapter 1",
            }],
        )
        s.add(run); await s.commit()
        sid, aid, rid = src.id, art.id, run.id
    with open(pdf_path_for(sid, str(tmp_path)), "wb") as fh:
        fh.write(_pdf())

    async def still_down(pdf_bytes, segment):
        return None

    monkeypatch.setattr(_pdf_import_mod, "escalate_segment", still_down)
    svc = FirecrawlService()
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        run = await s.get(ExtractionRun, rid)
        await _pdf_import_mod.retry_escalation(svc, s, src, run, rid)
        await s.commit()

    async with factory() as s:
        run = await s.get(ExtractionRun, rid)
        assert run.status == RunStatus.COMPLETED
        assert run.escalation_pending and len(run.escalation_pending) == 1  # still pending
        assert run.articles_updated == 0
        art = await s.get(Article, aid)
        assert art.content_markdown == "OLD"          # unchanged
