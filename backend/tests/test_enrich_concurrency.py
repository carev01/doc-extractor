"""enrich_run_images describes an article's images concurrently (bounded), not
one at a time — with DB writes still serialized on the single session."""
import asyncio
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
    buf = io.BytesIO()
    Image.effect_noise((w, h), 100).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def _seed_one_article_k_images(factory, k):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id); s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        body = "# A\n\n" + "".join(f"![p{i}](/media/PLACEHOLDER/img{i}.png)\n\n" for i in range(k))
        art = Article(source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                      title="A", source_url="https://x/a", topic_key="https://x/a",
                      content_markdown=body, content_hash="h")
        s.add(art); await s.flush()
        art.content_markdown = art.content_markdown.replace("/media/PLACEHOLDER/", f"/media/{art.id}/")
        d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
        for i in range(k):
            fn = f"img{i}.png"
            s.add(ArticleImage(article_id=art.id, original_url=f"https://x/p{i}.png",
                               local_filename=fn, local_path=f"/media/{art.id}/{fn}", sort_order=i))
            with open(os.path.join(d, fn), "wb") as fh:
                fh.write(_noise_png(400 + i * 10, 300 + i * 10))  # distinct → distinct sha
        await s.commit()
        return src.id, run.id


async def test_article_images_described_concurrently(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_concurrency", 4)
    src_id, run_id = await _seed_one_article_k_images(factory, 4)

    inflight = 0
    peak = 0

    async def fake(data, alt, **kw):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)  # simulate VLM latency so overlap is observable
        inflight -= 1
        return Desc(text="d", kind="other")

    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=fake, max_new=10**9)

    assert described == 4
    assert peak >= 2, f"expected concurrent describes, peak in-flight was {peak}"

    # All four images persisted their description.
    async with factory() as s:
        imgs = (await s.execute(select(ArticleImage))).scalars().all()
        assert len(imgs) == 4 and all(i.description == "d" for i in imgs)


async def test_unsupported_format_marked_not_meaningful_not_sent(factory):
    # An image stored as .png but whose bytes are WMF (Arcserve serves these) 400s
    # the VLM on every run. It must be dropped (is_meaningful=False), never sent.
    async with factory() as s:
        v = Vendor(name="Arcserve"); s.add(v); await s.flush()
        p = Product(name="Backup", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="Admin Guide", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        art = Article(source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
                      title="A", source_url="https://x/a", topic_key="https://x/a",
                      content_markdown="# A\n\n![p](/media/PLACEHOLDER/w.png)\n\n", content_hash="h")
        s.add(art); await s.flush()
        art.content_markdown = art.content_markdown.replace("/media/PLACEHOLDER/", f"/media/{art.id}/")
        # is_meaningful already True (as the stuck image is), description NULL.
        img = ArticleImage(article_id=art.id, original_url="https://x/w.png",
                           local_filename="w.png", local_path=f"/media/{art.id}/w.png",
                           sort_order=0, is_meaningful=True)
        s.add(img); await s.flush()
        img_id, src_id, run_id = img.id, src.id, run.id
        d = os.path.join(settings.media_dir, str(art.id)); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "w.png"), "wb") as fh:
            fh.write(b"\xd7\xcd\xc6\x9a" + b"\x00" * 5000)  # WMF placeable header
        await s.commit()

    async def boom(data, alt, **kw):
        raise AssertionError("describe must not be called for an unsupported-format image")

    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=boom, max_new=10**9)
    assert described == 0

    async with factory() as s:
        img = await s.get(ArticleImage, img_id)
        assert img.is_meaningful is False and img.description is None


async def test_concurrency_one_is_serial(factory, monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_concurrency", 1)
    src_id, run_id = await _seed_one_article_k_images(factory, 3)

    inflight = 0
    peak = 0

    async def fake(data, alt, **kw):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return Desc(text="d", kind="other")

    async with factory() as db:
        described = await enrich_run_images(db, src_id, run_id, describe=fake, max_new=10**9)

    assert described == 3
    assert peak == 1, f"concurrency=1 must stay serial, peak was {peak}"
