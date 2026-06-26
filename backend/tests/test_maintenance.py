import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
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
    orphan = _orphan(settings.media_dir)

    async with factory() as s:
        result = await maintenance.run_maintenance_sweeps(s)

    assert result["media_removed"] == 1          # the sweep actually ran
    assert result["purged_exports"] == 0         # export purge ran (nothing to purge)
    assert not os.path.exists(orphan)            # orphan dir gone


async def test_sweeps_gated_to_hourly(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    os.makedirs(settings.media_dir, exist_ok=True)
    maintenance._last_media_gc = None
    maintenance._last_export_purge = None
    now = datetime.now(timezone.utc)

    async with factory() as s:
        first = await maintenance.run_maintenance_sweeps(s, now=now)
        # Immediately again, same clock → not due → skipped (None sentinels).
        orphan = _orphan(settings.media_dir)
        second = await maintenance.run_maintenance_sweeps(s, now=now)

    assert first["media_removed"] is not None and first["purged_exports"] is not None
    assert second["media_removed"] is None and second["purged_exports"] is None
    assert os.path.exists(orphan)                # gated call did NOT sweep

    # An hour later it's due again.
    async with factory() as s:
        third = await maintenance.run_maintenance_sweeps(s, now=now + timedelta(hours=1, seconds=1))
    assert third["media_removed"] == 1
    assert not os.path.exists(orphan)
