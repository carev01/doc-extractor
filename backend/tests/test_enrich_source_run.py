"""enrich_source_run: worker entrypoint for kind='enrich' runs — drains ALL of a
source's missing images (no scrape, no per-run budget), commits a run_start floor,
and completes the run/source, firing extraction_complete when subscribed."""
import io
import os
import sys

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.extraction_run import RunStatus
from app.models.image import ArticleImage
from app.models.content_change import ContentChange
from app.services import image_describe
from app.services.firecrawl import FirecrawlService

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 100)
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    await engine.dispose()


def _noise_png(w, h):
    # Noise doesn't compress, so it clears image_min_bytes (a solid-color PNG is
    # ~1 KB, under the threshold, and would be wrongly rejected as decorative).
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def _seed_source_run_with_n_images(factory, n, kind="enrich"):
    """Seed one source + a RUNNING ExtractionRun of the given kind + n articles,
    each with content_hash='h-raw' and a meaningful noise-PNG image on disk.
    Returns (source_id, run_id)."""
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running", kind=kind); s.add(run); await s.flush()

        for i in range(n):
            filename = f"img{i}.png"
            art = Article(
                source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                title=f"A{i}", source_url=f"https://x/a{i}", topic_key=f"https://x/a{i}",
                content_markdown=f"# A{i}\n\n![pic](/media/PLACEHOLDER/{filename})\n\nBody.",
                content_hash="h-raw",
            )
            s.add(art); await s.flush()
            art.content_markdown = art.content_markdown.replace("/media/PLACEHOLDER/", f"/media/{art.id}/")
            img = ArticleImage(article_id=art.id, original_url=f"https://x/pic{i}.png",
                               local_filename=filename, local_path=f"/media/{art.id}/{filename}", sort_order=0)
            s.add(img)
            await s.flush()
            d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
            # Distinct dimensions per image -> distinct bytes/sha256, all meaningful.
            with open(os.path.join(d, filename), "wb") as fh:
                fh.write(_noise_png(400 + i * 10, 300 + i * 10))

        await s.commit()
        return src.id, run.id


async def test_enrich_source_run_drains_and_completes(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_enabled", True)
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 1)  # default budget tiny...

    async def fake(data, alt, **kw):
        return image_describe.ImageDescription(text="a diagram", kind="diagram")
    monkeypatch.setattr(image_describe, "describe_image", fake)

    src_id, run_id = await _seed_source_run_with_n_images(factory, 3, kind="enrich")
    svc = FirecrawlService()
    async with factory() as db:
        await svc.enrich_source_run(db, src_id, run_id)
        # enrich_source_run flushes its terminal state (mirrors retry_escalation_run's
        # convention); the caller (app.worker.run_one) commits after dispatch — replicate
        # that here since we're calling the service directly rather than via the worker.
        await db.commit()

    async with factory() as s:
        run = await s.get(ExtractionRun, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.articles_updated == 3            # ...but the enrich run drained ALL 3
        imgs = (await s.execute(select(ArticleImage).where(ArticleImage.article_id.in_(
            select(Article.id).where(Article.source_id == src_id))))).scalars().all()
        assert all(i.description is not None for i in imgs)
        # run_start floor row exists for this run
        rs = (await s.execute(select(ContentChange).where(
            ContentChange.run_id == run_id, ContentChange.change_type == "run_start"))).scalars().all()
        assert len(rs) == 1
        # Article.content_hash was NOT modified by enrichment
        art = (await s.execute(select(Article).where(Article.source_id == src_id))).scalars().first()
        assert art.content_hash == "h-raw"
