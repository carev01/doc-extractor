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
from app.models.image import ArticleImage
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


def _pix(rgb):
    p = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64)); p.set_rect(p.irect, rgb)
    return p


def _pdf(color=(255, 0, 0)) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Section with a figure")
    page.insert_image(fitz.Rect(72, 100, 200, 200), pixmap=_pix(color))
    doc.set_toc([[1, "Figure section", 1]])
    return doc.tobytes()


async def _source(factory, tmp_path) -> uuid.UUID:
    settings.media_dir = str(tmp_path / "media")
    settings.pdf_dir = str(tmp_path / "pdf")
    os.makedirs(settings.pdf_dir, exist_ok=True)
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf")
        s.add(src); await s.commit()
        return src.id


async def _run(factory, sid) -> uuid.UUID:
    svc = FirecrawlService()
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        run = ExtractionRun(source_id=sid); s.add(run); await s.flush()
        rid = run.id
        await run_pdf_extraction(svc, s, src, run, rid)
        await s.commit()
    return rid


async def test_pdf_image_persisted_and_served(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf())
    await _run(factory, sid)

    async with factory() as s:
        art = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalar_one()
        imgs = (await s.execute(
            select(ArticleImage).where(ArticleImage.article_id == art.id))).scalars().all()
        assert len(imgs) == 1
        fname = imgs[0].local_filename
        served = f"{settings.media_url_prefix}/{art.id}/{fname}"
        assert imgs[0].local_path == served
        assert served in art.content_markdown          # rewritten to served URL
        assert os.path.isfile(os.path.join(settings.media_dir, str(art.id), fname))


async def test_rerun_same_image_is_unchanged(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf())
    await _run(factory, sid)
    rid2 = await _run(factory, sid)
    async with factory() as s:
        r = await s.get(ExtractionRun, rid2)
        assert r.articles_unchanged == 1 and r.articles_extracted == 0


async def test_changed_image_clears_old_file(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf(color=(255, 0, 0)))
    await _run(factory, sid)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf(color=(0, 0, 255)))   # different image bytes → new sha
    await _run(factory, sid)
    async with factory() as s:
        art = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalar_one()
    # Only the current image remains in the article's media dir.
    files = os.listdir(os.path.join(settings.media_dir, str(art.id)))
    assert len(files) == 1


def _pdf_no_image() -> bytes:
    """Same section title as _pdf() (so the article matches by topic_key) but the
    figure is gone — an update that drops all images."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Section with a figure, now text only and changed")
    doc.set_toc([[1, "Figure section", 1]])
    return doc.tobytes()


async def test_update_that_removes_all_images_clears_dir(factory, tmp_path):
    sid = await _source(factory, tmp_path)
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf())
    await _run(factory, sid)
    async with factory() as s:
        art = (await s.execute(
            select(Article).where(Article.source_id == sid))).scalar_one()
        art_id = art.id
    art_dir = os.path.join(settings.media_dir, str(art_id))
    assert os.listdir(art_dir) == [os.listdir(art_dir)[0]]  # image present pre-update

    # Re-extract a figure-less version of the same section → updated, no images.
    with open(pdf_path_for(sid, settings.pdf_dir), "wb") as fh:
        fh.write(_pdf_no_image())
    await _run(factory, sid)

    async with factory() as s:
        imgs = (await s.execute(
            select(ArticleImage).where(ArticleImage.article_id == art_id))).scalars().all()
        assert imgs == []                       # ArticleImage rows gone
    # The stale image file must be gone (dir cleared / removed).
    assert not os.path.isdir(art_dir) or os.listdir(art_dir) == []
