"""Incremental runs must ignore volatile signed-image-URL tokens.

Document360/Azure serve images through short-lived SAS URLs whose st/se/sig query
params are re-minted on every scrape. Those tokens reference the *same* image, so
a re-scrape of an otherwise-identical page must be classified "unchanged" — not
"updated", which wipes every ArticleImage and needlessly re-runs VLM enrichment.
This was the cause of ~60-90% of Securiti images being re-enriched each run."""

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
from app.services import firecrawl as fc
from app.services.firecrawl import firecrawl_service, _normalize_for_change_hash

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

# A valid 1x1 PNG.
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDw"
            "AChwGA60e6kgAAAABJRU5ErkJggg==")


# --- pure unit: fingerprint normalization ---

def test_normalize_strips_signed_image_query():
    # Two scrapes of the same image with freshly-minted SAS tokens normalise equal.
    a = ("![x](https://cdn.us.document360.io/a/1(2).png"
         "?sv=2026-02-06&st=2026-07-24T13%3A18%3A39Z&se=z&sig=abc%2Bdef%3D)")
    b = ("![x](https://cdn.us.document360.io/a/1(2).png"
         "?sv=2026-02-06&st=2026-07-24T18%3A00%3A00Z&se=y&sig=zzz%3D)")
    assert _normalize_for_change_hash(a) == _normalize_for_change_hash(b)
    # The path (incl. its parens) is preserved; only the query is dropped.
    assert _normalize_for_change_hash(a) == "![x](https://cdn.us.document360.io/a/1(2).png)"


def test_normalize_leaves_content_alone():
    md = "![x](https://cdn/img.png)\n\nProse mentioning ?foo=bar (not an image URL)."
    assert _normalize_for_change_hash(md) == md


# --- integration: a refreshed SAS token is not a content change ---

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


def _page(token):
    url = f"https://cdn.us.document360.io/a/1(2).png?sv=x&st={token}&sig={token}"
    md = f"# P\n\nStable body text.\n\n![shot]({url})\n"
    html = f'<h1>P</h1><p>Stable body text.</p><p><img src="{url}" alt="shot"></p>'
    return md, html


@pytest.mark.asyncio
async def test_refreshed_sas_token_scrapes_as_unchanged(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))

    # Content-addressed download without hitting the network (identical bytes each
    # call → identical filename, mirroring the real _download_image).
    async def fake_dl(self, img_url, article_dir, auth_cookies=None, user_agent=None):
        os.makedirs(article_dir, exist_ok=True)
        open(os.path.join(article_dir, "cafecafecafecafe.png"), "wb").close()
        return "cafecafecafecafe.png"
    monkeypatch.setattr(fc.FirecrawlService, "_download_image", fake_dl)

    src_id, run_id = await _source_and_run(factory)
    page_url = "https://helpcenter.securiti.ai/docs/x"

    md1, html1 = _page("TOKEN-1")
    async with factory() as db:
        r1 = await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id, url=page_url,
            markdown_content=md1, doc_html=html1,
            toc_entry_id=None, sort_order=0, title="P")
    assert r1 == "new"

    # Same page, only the SAS token differs.
    md2, html2 = _page("TOKEN-2")
    async with factory() as db:
        r2 = await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id, url=page_url,
            markdown_content=md2, doc_html=html2,
            toc_entry_id=None, sort_order=0, title="P")
    assert r2 == "unchanged", "a refreshed SAS token must not count as a content change"


@pytest.mark.asyncio
async def test_removed_image_is_pruned_but_transient_failure_keeps_files(
    factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    page_url = "https://helpcenter.securiti.ai/docs/y"

    async def ok_dl(self, img_url, article_dir, auth_cookies=None, user_agent=None):
        os.makedirs(article_dir, exist_ok=True)
        open(os.path.join(article_dir, "deadbeefdeadbeef.png"), "wb").close()
        return "deadbeefdeadbeef.png"

    monkeypatch.setattr(fc.FirecrawlService, "_download_image", ok_dl)
    src_id, run_id = await _source_and_run(factory)

    # 1) First scrape with one image → file on disk.
    md1, html1 = _page("T1")
    async with factory() as db:
        await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id, url=page_url,
            markdown_content=md1, doc_html=html1,
            toc_entry_id=None, sort_order=0, title="P")
    art_id = None
    async with factory() as db:
        art_id = (await db.execute(select(Article).where(Article.source_id == src_id))).scalar_one().id
    art_dir = os.path.join(str(tmp_path), str(art_id))
    assert os.listdir(art_dir) == ["deadbeefdeadbeef.png"]

    # 2) Page changes and the image is gone → the orphaned file is pruned.
    async with factory() as db:
        await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id, url=page_url,
            markdown_content="# P\n\nNow with different text and no image.\n",
            doc_html="<h1>P</h1><p>Now with different text and no image.</p>",
            toc_entry_id=None, sort_order=0, title="P")
    assert os.listdir(art_dir) == [], "a removed image's file must be pruned"

    # 3) Page changes again WITH an image, but the download fails transiently.
    #    Re-seed a prior file, then scrape with a failing downloader.
    open(os.path.join(art_dir, "deadbeefdeadbeef.png"), "wb").close()

    async def fail_dl(self, img_url, article_dir, auth_cookies=None, user_agent=None):
        return None

    monkeypatch.setattr(fc.FirecrawlService, "_download_image", fail_dl)
    md3, html3 = _page("T3")
    async with factory() as db:
        await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id, url=page_url,
            markdown_content=md3 + "\n\nedited\n", doc_html=html3,
            toc_entry_id=None, sort_order=0, title="P")
    assert os.listdir(art_dir) == ["deadbeefdeadbeef.png"], (
        "a total download failure must NOT wipe the article's existing images")
