"""enrich_run_images: max_new budget override + returns the count of described images."""
import io
import os
import sys

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.image import ArticleImage
from app.services.image_describe import enrich_run_images, ImageDescription as Desc

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


async def _seed_source_with_n_images(factory, n):
    """Seed one source with n articles, each with a distinct meaningful noise-PNG
    image written to disk. Returns (source_id, run_id)."""
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()

        for i in range(n):
            filename = f"img{i}.png"
            art = Article(
                source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                title=f"A{i}", source_url=f"https://x/a{i}", topic_key=f"https://x/a{i}",
                content_markdown=f"# A{i}\n\n![pic](/media/PLACEHOLDER/{filename})\n\nBody.",
                content_hash=f"h-raw-{i}",
            )
            s.add(art); await s.flush()
            art.content_markdown = art.content_markdown.replace("/media/PLACEHOLDER/", f"/media/{art.id}/")
            img = ArticleImage(article_id=art.id, original_url=f"https://x/pic{i}.png",
                               local_filename=filename, local_path=f"/media/{art.id}/{filename}", sort_order=0)
            s.add(img)
            await s.flush()
            d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
            # Distinct dimensions per image → distinct bytes/sha256, all meaningful.
            with open(os.path.join(d, filename), "wb") as fh:
                fh.write(_noise_png(400 + i * 10, 300 + i * 10))

        await s.commit()
        return src.id, run.id


async def test_max_new_unlimited_drains_all_and_returns_count(factory, monkeypatch):
    # Default budget is 2, but max_new=None-override (a big number) describes all 3.
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 2)
    src_id, run_id = await _seed_source_with_n_images(factory, 3)
    calls = {"n": 0}

    async def fake(data, alt, **kw):
        calls["n"] += 1
        return Desc(text=f"d{calls['n']}", kind="other")

    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=fake, max_new=10**9)
    assert described == 3 and calls["n"] == 3


async def test_default_still_respects_budget(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 2)
    src_id, run_id = await _seed_source_with_n_images(factory, 3)
    calls = {"n": 0}

    async def fake(data, alt, **kw):
        calls["n"] += 1
        return Desc(text="d", kind="other")

    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=fake)  # max_new omitted
    assert described == 2 and calls["n"] == 2


async def test_missing_file_image_marked_not_meaningful(factory):
    # An ArticleImage whose bytes are missing on disk must be marked is_meaningful=False
    # (dropped from the pending backlog), not left NULL (which would keep the source in
    # the backlog and re-queue no-op enrich runs forever).
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        art = Article(source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                      title="A", source_url="https://x/a", topic_key="https://x/a",
                      content_markdown="# A", content_hash="h")
        s.add(art); await s.flush()
        img = ArticleImage(article_id=art.id, original_url="u", local_filename="gone.png",
                           local_path=f"/media/{art.id}/gone.png", sort_order=0)
        s.add(img); await s.flush()
        img_id, src_id, run_id = img.id, src.id, run.id
        await s.commit()
        # (no file written to media_dir/<art.id>/gone.png)

    async def boom(data, alt, **kw):
        raise AssertionError("describe must not be called for a missing-file image")

    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=boom)
    assert described == 0

    async with factory() as s:
        img = await s.get(ArticleImage, img_id)
        assert img.is_meaningful is False and img.description is None
