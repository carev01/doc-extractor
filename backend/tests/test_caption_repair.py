"""repair_missing_captions: heal described images whose caption never landed.

An image described while inject_caption couldn't match its reference (markdown
title on the reference, e.g. AvePoint) keeps its description and stays out of the
"pending enrichment" backlog, so nothing would ever put the caption into the
content. The enrichment phase repairs those from the stored descriptions, without
any VLM call.
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
from app.models.image import ArticleImage
from app.services.image_describe import enrich_run_images

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

DESC = "Screenshot of the rapid recovery jobs screen."


@pytest_asyncio.fixture
async def factory(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    await engine.dispose()


async def _seed(f, *, reference: str, captioned: bool):
    """One article whose single image is already described. `reference` is a
    printf-style template taking the served /media path."""
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        art = Article(
            source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
            title="Rapid Recovery Jobs", source_url="https://x/a", topic_key="https://x/a",
            content_markdown="placeholder", content_hash="h-raw",
        )
        s.add(art); await s.flush()
        served = f"/media/{art.id}/x.png"
        body = f"# A\n\n{reference % served}\n"
        if captioned:
            body += f"\n> **Figure:** {DESC}\n"
        art.content_markdown = body + "\nBody."
        s.add(ArticleImage(
            article_id=art.id, original_url="https://x/pic.png", local_filename="x.png",
            local_path=served, sort_order=0, is_meaningful=True, description=DESC,
            bytes_sha256="s" * 64,
        ))
        await s.commit()
        return src.id, art.id, run.id


async def _enrich(f, src_id, run_id):
    async def never(*a, **kw):  # a repair must not reach the VLM
        raise AssertionError("describe_image called during caption repair")
    async with f() as db:
        return await enrich_run_images(db, src_id, run_id, describe=never)


async def _markdown(f, art_id):
    async with f() as s:
        return (await s.get(Article, art_id)).content_markdown


async def _updated_rows(f, art_id):
    async with f() as s:
        return (await s.execute(
            select(ContentChange).where(
                ContentChange.article_id == art_id, ContentChange.change_type == "updated")
        )).scalars().all()


async def test_repairs_titled_reference_without_vlm_call(factory):
    src_id, art_id, run_id = await _seed(
        factory, reference='![Rapid recovery jobs](%s "Rapid recovery jobs.")', captioned=False)

    described = await _enrich(factory, src_id, run_id)

    assert described == 0            # nothing newly described — this is a repair
    md = await _markdown(factory, art_id)
    assert f"> **Figure:** {DESC}" in md
    assert '"Rapid recovery jobs."' in md      # the title is preserved
    assert len(await _updated_rows(factory, art_id)) == 1


async def test_repair_is_write_free_second_time(factory):
    src_id, art_id, run_id = await _seed(
        factory, reference='![t](%s "A title")', captioned=False)
    await _enrich(factory, src_id, run_id)
    first = await _markdown(factory, art_id)

    async with factory() as s:
        # Retire run 1 first — uq_active_run_per_source allows only one live run.
        (await s.get(ExtractionRun, run_id)).status = "completed"
        run2 = ExtractionRun(source_id=src_id, status="running")
        s.add(run2); await s.flush(); run2_id = run2.id
        await s.commit()
    await _enrich(factory, src_id, run2_id)

    # Idempotent: identical markdown and no second 'updated' row (which would
    # show downstream consumers a phantom change on every run).
    assert await _markdown(factory, art_id) == first
    assert len(await _updated_rows(factory, art_id)) == 1


async def test_healthy_captioned_article_is_untouched(factory):
    src_id, art_id, run_id = await _seed(factory, reference="![t](%s)", captioned=True)
    before = await _markdown(factory, art_id)

    await _enrich(factory, src_id, run_id)

    assert await _markdown(factory, art_id) == before
    assert await _updated_rows(factory, art_id) == []
