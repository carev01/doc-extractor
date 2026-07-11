"""enrich_run_images: describe → cache → caption → updated outbox row, best-effort."""
import io
import os
import sys
import uuid

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
from app.models.image import ArticleImage
from app.models.image_description import ImageDescription
from app.models.content_change import ContentChange
from app.services import image_describe
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


def _png(w, h, color=(20, 40, 60)):
    buf = io.BytesIO(); Image.new("RGB", (w, h), color).save(buf, format="PNG"); return buf.getvalue()


def _noise_png(w, h):
    # Noise doesn't compress, so it clears image_min_bytes (a solid-color PNG is
    # ~1 KB, under the threshold, and would be wrongly rejected as decorative).
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def _seed_article_with_image(f, *, img_bytes, filename="x.png", md=None):
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        art = Article(
            source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
            title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown=md or f"# A\n\n![pic](/media/PLACEHOLDER/{filename})\n\nBody.",
            content_hash="h-raw",
        )
        s.add(art); await s.flush()
        served = f"/media/{art.id}/{filename}"
        art.content_markdown = art.content_markdown.replace("/media/PLACEHOLDER/", f"/media/{art.id}/")
        img = ArticleImage(article_id=art.id, original_url="https://x/pic.png",
                           local_filename=filename, local_path=served, sort_order=0)
        s.add(img)
        await s.commit()
        # Write bytes to media_dir/<article.id>/<filename>
        d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, filename), "wb") as fh:
            fh.write(img_bytes)
        return src.id, art.id, run.id


async def test_describes_and_captions_and_emits_updated_row(factory):
    src_id, art_id, run_id = await _seed_article_with_image(factory, img_bytes=_noise_png(400, 300))
    calls = {"n": 0}

    async def fake_describe(data, alt, **kw):
        calls["n"] += 1
        return Desc(text="A topology diagram.", kind="diagram")

    await enrich_run_images_via(factory, src_id, run_id, fake_describe)

    async with factory() as s:
        img = (await s.execute(select(ArticleImage).where(ArticleImage.article_id == art_id))).scalar_one()
        assert img.is_meaningful is True and img.description == "A topology diagram." and img.kind == "diagram"
        assert img.bytes_sha256 and img.width == 400 and img.height == 300
        art = await s.get(Article, art_id)
        assert "> **Figure:** A topology diagram." in art.content_markdown
        assert art.content_hash == "h-raw"  # untouched
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.article_id == art_id, ContentChange.change_type == "updated"))).scalars().all()
        assert len(rows) == 1
        cached = await s.get(ImageDescription, img.bytes_sha256)
        assert cached is not None and cached.description == "A topology diagram."
    assert calls["n"] == 1


async def test_cache_hit_skips_vlm_call(factory):
    # Two articles sharing identical image bytes → described once, reused.
    img = _noise_png(400, 300)
    src_id, a1, run_id = await _seed_article_with_image(factory, img_bytes=img, filename="a.png")
    # second article in the same source with the same bytes
    async with factory() as s:
        art2 = Article(source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
                       title="B", source_url="https://x/b", topic_key="https://x/b",
                       content_markdown="# B\n\n![p](/media/PH/b.png)\n\nx.", content_hash="h2")
        s.add(art2); await s.flush()
        art2.content_markdown = art2.content_markdown.replace("/media/PH/", f"/media/{art2.id}/")
        s.add(ArticleImage(article_id=art2.id, original_url="u", local_filename="b.png",
                           local_path=f"/media/{art2.id}/b.png", sort_order=0))
        await s.commit()
        d = os.path.join(settings.media_dir, str(art2.id)); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "b.png"), "wb").write(img)

    calls = {"n": 0}
    async def fake_describe(data, alt, **kw):
        calls["n"] += 1
        return Desc(text="Shared image.", kind="photo")

    await enrich_run_images_via(factory, src_id, run_id, fake_describe)
    assert calls["n"] == 1  # described once despite two articles


async def test_idempotent_second_run_no_vlm_no_new_row(factory):
    src_id, art_id, run_id = await _seed_article_with_image(factory, img_bytes=_noise_png(400, 300))
    async def fake(data, alt, **kw):
        return Desc(text="Desc.", kind="screenshot")
    await enrich_run_images_via(factory, src_id, run_id, fake)

    calls = {"n": 0}
    async def counting(data, alt, **kw):
        calls["n"] += 1; return Desc(text="Desc.", kind="screenshot")
    await enrich_run_images_via(factory, src_id, run_id, counting)

    assert calls["n"] == 0  # all images already described
    async with factory() as s:
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.article_id == art_id, ContentChange.change_type == "updated"))).scalars().all()
        assert len(rows) == 1  # only the first run's row


async def test_budget_cap_defers(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_max_per_run", 1)
    src_id, a1, run_id = await _seed_article_with_image(factory, img_bytes=_noise_png(400, 300), filename="a.png")
    async with factory() as s:  # second article, distinct bytes
        art2 = Article(source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
                       title="B", source_url="https://x/b", topic_key="https://x/b",
                       content_markdown="![p](/media/PH/b.png)", content_hash="h2")
        s.add(art2); await s.flush()
        art2.content_markdown = f"![p](/media/{art2.id}/b.png)"
        s.add(ArticleImage(article_id=art2.id, original_url="u", local_filename="b.png",
                           local_path=f"/media/{art2.id}/b.png", sort_order=0))
        await s.commit()
        d = os.path.join(settings.media_dir, str(art2.id)); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "b.png"), "wb").write(_noise_png(400, 300))

    calls = {"n": 0}
    async def fake(data, alt, **kw):
        calls["n"] += 1; return Desc(text=f"d{calls['n']}", kind="other")
    await enrich_run_images_via(factory, src_id, run_id, fake)
    assert calls["n"] == 1  # budget of 1 honored; the other image deferred (still undescribed)


async def test_not_meaningful_image_no_row(factory):
    src_id, art_id, run_id = await _seed_article_with_image(factory, img_bytes=_png(30, 30))
    async def fake(data, alt, **kw):
        raise AssertionError("should not be called for a tiny image")
    await enrich_run_images_via(factory, src_id, run_id, fake)
    async with factory() as s:
        img = (await s.execute(select(ArticleImage).where(ArticleImage.article_id == art_id))).scalar_one()
        assert img.is_meaningful is False and img.description is None
        rows = (await s.execute(select(ContentChange).where(ContentChange.change_type == "updated"))).scalars().all()
        assert rows == []


# helper: run the phase on its own session against the test factory
async def enrich_run_images_via(factory, src_id, run_id, describe):
    async with factory() as db:
        await enrich_run_images(db, src_id, run_id, describe=describe)


def _noise_jpeg(w, h):
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def test_non_png_image_sent_with_correct_mime(factory):
    # A .jpg image must be described with mime image/jpeg, not the default image/png.
    src_id, art_id, run_id = await _seed_article_with_image(
        factory, img_bytes=_noise_jpeg(400, 300), filename="pic.jpg")
    seen = {}

    async def fake(data, alt, *, mime="image/png", **kw):
        seen["mime"] = mime
        return Desc(text="A screenshot.", kind="screenshot")

    await enrich_run_images_via(factory, src_id, run_id, fake)
    assert seen.get("mime") == "image/jpeg"


async def test_circuit_breaker_stops_after_consecutive_failures(factory, monkeypatch):
    # With the failure cap at 2 and describe always returning None, the phase stops
    # calling the VLM after 2 consecutive failures rather than trying every image.
    monkeypatch.setattr(settings, "image_vlm_max_consecutive_failures", 2)
    src_id, _a, run_id = await _seed_article_with_image(
        factory, img_bytes=_noise_png(400, 300), filename="a.png")
    # Two more articles in the same source, each with a distinct meaningful image.
    for i, fn in enumerate(("b.png", "c.png")):
        async with factory() as s:
            art = Article(source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
                          title=f"T{i}", source_url=f"https://x/{fn}", topic_key=f"https://x/{fn}",
                          content_markdown=f"![p](/media/PH/{fn})", content_hash=f"h{i}")
            s.add(art); await s.flush()
            art.content_markdown = f"![p](/media/{art.id}/{fn})"
            s.add(ArticleImage(article_id=art.id, original_url="u", local_filename=fn,
                               local_path=f"/media/{art.id}/{fn}", sort_order=0))
            await s.commit()
            d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, fn), "wb").write(_noise_png(400, 300))

    calls = {"n": 0}
    async def failing(data, alt, **kw):
        calls["n"] += 1
        return None

    await enrich_run_images_via(factory, src_id, run_id, failing)
    assert calls["n"] == 2  # stopped at the consecutive-failure cap, didn't try all three
