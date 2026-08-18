import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

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
from app.services.media_gc import backfill_image_sizes
import app.services.maintenance as maintenance

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


def _orphan(media_dir: str) -> str:
    d = os.path.join(media_dir, str(uuid.uuid4()))
    os.makedirs(d, exist_ok=True)
    return d


async def test_runs_media_gc_when_due(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    os.makedirs(settings.media_dir, exist_ok=True)
    maintenance._last_media_gc = None
    maintenance._last_export_purge = None
    maintenance._last_size_backfill = None
    orphan = _orphan(settings.media_dir)

    async with factory() as s:
        result = await maintenance.run_maintenance_sweeps(s)

    assert result["media_removed"] == 1          # the sweep actually ran
    assert result["purged_exports"] == 0         # export purge ran (nothing to purge)
    assert result["image_sizes_filled"] == 0     # size backfill ran (nothing to fill)
    assert not os.path.exists(orphan)            # orphan dir gone


async def test_sweeps_gated_to_hourly(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    os.makedirs(settings.media_dir, exist_ok=True)
    maintenance._last_media_gc = None
    maintenance._last_export_purge = None
    maintenance._last_size_backfill = None
    now = datetime.now(timezone.utc)

    async with factory() as s:
        first = await maintenance.run_maintenance_sweeps(s, now=now)
        # Immediately again, same clock → not due → skipped (None sentinels).
        orphan = _orphan(settings.media_dir)
        second = await maintenance.run_maintenance_sweeps(s, now=now)

    assert first["media_removed"] is not None and first["purged_exports"] is not None
    assert first["image_sizes_filled"] is not None
    assert second["media_removed"] is None and second["purged_exports"] is None
    assert second["image_sizes_filled"] is None
    assert os.path.exists(orphan)                # gated call did NOT sweep

    # An hour later it's due again.
    async with factory() as s:
        third = await maintenance.run_maintenance_sweeps(s, now=now + timedelta(hours=1, seconds=1))
    assert third["media_removed"] == 1
    assert not os.path.exists(orphan)


async def test_backfill_fills_zeroed_image_sizes(factory, tmp_path, monkeypatch):
    """file_size_bytes rows left at the column default get their real size; a row
    whose file is gone keeps 0 (an honest 'unknown', not a fabricated size)."""
    media = str(tmp_path / "media")
    monkeypatch.setattr(settings, "media_dir", media)
    os.makedirs(media, exist_ok=True)

    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status="running"); s.add(run); await s.flush()
        art = Article(
            source_id=src.id, extraction_run_id=run.id, created_run_id=run.id,
            title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown="body", content_hash="h",
        )
        s.add(art); await s.flush()
        art_dir = os.path.join(media, str(art.id))
        os.makedirs(art_dir, exist_ok=True)
        with open(os.path.join(art_dir, "on-disk.png"), "wb") as fh:
            fh.write(b"x" * 4096)
        for fn, size in (("on-disk.png", 0), ("gone.png", 0), ("already.png", 123)):
            s.add(ArticleImage(
                article_id=art.id, original_url=f"https://x/{fn}", local_filename=fn,
                local_path=f"/media/{art.id}/{fn}", sort_order=0, file_size_bytes=size,
            ))
        await s.commit()
        art_id = art.id

    async with factory() as s:
        assert await backfill_image_sizes(s, media) == 1

    async with factory() as s:
        sizes = {
            i.local_filename: i.file_size_bytes
            for i in (await s.execute(
                select(ArticleImage).where(ArticleImage.article_id == art_id)
            )).scalars()
        }
    assert sizes == {"on-disk.png": 4096, "gone.png": 0, "already.png": 123}

    # Converges: the filled row no longer matches, so a second sweep is a no-op.
    async with factory() as s:
        assert await backfill_image_sizes(s, media) == 0
