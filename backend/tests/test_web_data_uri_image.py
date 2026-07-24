"""A base64 data:image in web content must be extracted to a stored file, never
left inline in the stored markdown (it bloated Securiti exports otherwise)."""

import os
import sys
import uuid

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
from app.models.image import ArticleImage
from app.services.firecrawl import firecrawl_service

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

# A valid 1x1 PNG.
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDw"
            "AChwGA60e6kgAAAABJRU5ErkJggg==")


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


async def _source_and_run(f):
    async with f() as s:
        v = Vendor(name="Securiti"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Help"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Help Center",
                                  base_url="https://helpcenter.securiti.ai/docs")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.commit()
        return src.id, run.id


async def test_data_uri_image_extracted_not_inlined(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id, run_id = await _source_and_run(factory)
    data_uri = f"data:image/png;base64,{_PNG_B64}"
    md = f"# Page\n\nSome text.\n\n![shot]({data_uri})\n"
    html = f'<h1>Page</h1><p>Some text.</p><p><img src="{data_uri}" alt="shot"></p>'

    async with factory() as db:
        await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id,
            url="https://helpcenter.securiti.ai/docs/x",
            markdown_content=md, doc_html=html,
            toc_entry_id=None, sort_order=0, title="Page",
        )

    async with factory() as db:
        art = (await db.execute(select(Article).where(Article.source_id == src_id))).scalar_one()
        # base64 gone from the stored markdown; rewritten to a /media link
        assert "data:image" not in art.content_markdown
        assert f"{settings.media_url_prefix}/{art.id}/" in art.content_markdown
        imgs = (await db.execute(
            select(ArticleImage).where(ArticleImage.article_id == art.id))).scalars().all()
        assert len(imgs) == 1
        assert imgs[0].original_url == "data:image"
        # the decoded bytes were written to the article's media dir
        art_dir = os.path.join(str(tmp_path), str(art.id))
        assert os.path.isdir(art_dir) and os.listdir(art_dir)


async def test_data_uri_example_in_prose_is_left_alone(factory, tmp_path, monkeypatch):
    # A data: URI shown as example TEXT (not an <img>) is legitimate content and
    # must be preserved — only real <img> data URIs are extracted.
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    src_id, run_id = await _source_and_run(factory)
    md = "# API\n\nSend an image as `data:image/png;base64,iVBORw0KGgo...`.\n"
    html = "<h1>API</h1><p>Send an image as <code>data:image/png;base64,iVBORw0KGgo...</code>.</p>"

    async with factory() as db:
        await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id,
            url="https://helpcenter.securiti.ai/docs/api",
            markdown_content=md, doc_html=html,
            toc_entry_id=None, sort_order=0, title="API",
        )

    async with factory() as db:
        art = (await db.execute(select(Article).where(Article.source_id == src_id))).scalar_one()
        assert "data:image/png;base64,iVBORw0KGgo..." in art.content_markdown  # untouched
