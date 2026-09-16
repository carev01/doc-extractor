"""Tests for purge_expired_exports — age sweep + size-cap eviction."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExportJob
from app.models.export_job import ExportStatus
from app.services.export_retention import purge_expired_exports

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(db) -> uuid.UUID:
    v = Vendor(name="V")
    db.add(v)
    await db.flush()
    s_prod = Product(vendor_id=v.id, name="P")
    db.add(s_prod)
    await db.flush()
    s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s.id


def _make_export_dir(export_dir: str, export_id: uuid.UUID, nbytes: int) -> str:
    sub = os.path.join(export_dir, str(export_id))
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "out.pdf"), "wb") as f:
        f.write(b"x" * nbytes)
    return sub


async def _add_job(db, source_id, created_at, nbytes, export_dir, status=ExportStatus.COMPLETED):
    eid = uuid.uuid4()
    _make_export_dir(export_dir, eid, nbytes)
    job = ExportJob(
        source_id=source_id, request={"format": "pdf"}, status=status,
        export_id=eid, result={"total_size_bytes": nbytes},
    )
    db.add(job)
    await db.flush()
    # created_at has a server_default, so set it explicitly post-insert.
    job.created_at = created_at
    await db.commit()
    return job.id, eid


@pytest.mark.asyncio
async def test_age_sweep_removes_old_keeps_recent(sessions, tmp_path):
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        now = datetime.now(timezone.utc)
        old_id, old_eid = await _add_job(db, sid, now - timedelta(days=10), 100, export_dir)
        new_id, new_eid = await _add_job(db, sid, now - timedelta(days=1), 100, export_dir)

        purged = await purge_expired_exports(db, export_dir, retention_days=7, max_total_bytes=0, now=now)

        assert purged == 1
        assert not os.path.isdir(os.path.join(export_dir, str(old_eid)))
        assert os.path.isdir(os.path.join(export_dir, str(new_eid)))
        remaining = (await db.execute(select(ExportJob.id))).scalars().all()
        assert remaining == [new_id]


@pytest.mark.asyncio
async def test_size_cap_evicts_oldest_first(sessions, tmp_path):
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        now = datetime.now(timezone.utc)
        # Three recent exports of 1000 bytes each = 3000; cap at 2500 → evict 1 (oldest).
        oldest_id, oldest_eid = await _add_job(db, sid, now - timedelta(hours=3), 1000, export_dir)
        mid_id, _ = await _add_job(db, sid, now - timedelta(hours=2), 1000, export_dir)
        new_id, _ = await _add_job(db, sid, now - timedelta(hours=1), 1000, export_dir)

        purged = await purge_expired_exports(db, export_dir, retention_days=0, max_total_bytes=2500, now=now)

        assert purged == 1
        assert not os.path.isdir(os.path.join(export_dir, str(oldest_eid)))
        remaining = set((await db.execute(select(ExportJob.id))).scalars().all())
        assert remaining == {mid_id, new_id}


@pytest.mark.asyncio
async def test_idempotent_and_missing_dir_tolerated(sessions, tmp_path):
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        now = datetime.now(timezone.utc)
        await _add_job(db, sid, now - timedelta(days=10), 100, export_dir)
        # First sweep removes it; second sweep is a no-op (no rows, dir already gone).
        assert await purge_expired_exports(db, export_dir, 7, 0, now=now) == 1
        assert await purge_expired_exports(db, export_dir, 7, 0, now=now) == 0


# ── Cached product-export zips (batch-<uuid> directories) ────────────────────
# These carry no export_jobs row and their name is not a bare UUID, so the orphan
# sweep's uuid.UUID(entry.name) guard skipped them entirely: they were immortal
# AND invisible to the size cap. They are also the only regenerable thing in the
# directory, which is why the cap spends them before evicting a real export.

def _make_batch_dir(export_dir: str, batch_id: uuid.UUID, nbytes: int, age_days: float = 0):
    path = os.path.join(export_dir, f"batch-{batch_id}")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "Vendor_Product.zip"), "wb") as f:
        f.write(b"z" * nbytes)
    if age_days:
        old = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
        os.utime(path, (old, old))
    return path


async def _add_batch_job(db, source_id, batch_id, export_dir, nbytes=10):
    eid = uuid.uuid4()
    _make_export_dir(export_dir, eid, nbytes)
    job = ExportJob(
        source_id=source_id, request={"format": "markdown"},
        status=ExportStatus.COMPLETED, export_id=eid,
        result={"total_size_bytes": nbytes},
        batch_id=batch_id, batch_seq=0, batch_label="V / P",
    )
    db.add(job)
    await db.commit()
    return job.id


@pytest.mark.asyncio
async def test_age_sweep_removes_a_stale_batch_cache(sessions, tmp_path):
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        now = datetime.now(timezone.utc)
        batch_id = uuid.uuid4()
        await _add_batch_job(db, sid, batch_id, export_dir)
        stale = _make_batch_dir(export_dir, batch_id, 100, age_days=10)

        await purge_expired_exports(
            db, export_dir, retention_days=7, max_total_bytes=0, now=now
        )
        assert not os.path.isdir(stale), (
            "batch-<uuid> is not a bare UUID, so the orphan sweep skipped it"
        )


@pytest.mark.asyncio
async def test_a_fresh_batch_cache_is_kept(sessions, tmp_path):
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        batch_id = uuid.uuid4()
        await _add_batch_job(db, sid, batch_id, export_dir)
        fresh = _make_batch_dir(export_dir, batch_id, 100)

        await purge_expired_exports(
            db, export_dir, retention_days=7, max_total_bytes=0,
            now=datetime.now(timezone.utc),
        )
        assert os.path.isdir(fresh)


@pytest.mark.asyncio
async def test_a_batch_cache_whose_jobs_are_gone_is_dropped(sessions, tmp_path):
    """Nothing left to rebuild it from, and nothing referencing it."""
    export_dir = str(tmp_path)
    async with sessions() as db:
        dead = _make_batch_dir(export_dir, uuid.uuid4(), 100)  # no jobs at all
        await purge_expired_exports(
            db, export_dir, retention_days=7, max_total_bytes=0,
            now=datetime.now(timezone.utc),
        )
        assert not os.path.isdir(dead)


@pytest.mark.asyncio
async def test_size_cap_counts_batch_caches_and_spends_them_first(sessions, tmp_path):
    """A batch zip rebuilds itself on the next download; a child export does not.
    So the cap drops the cache before it destroys content."""
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        now = datetime.now(timezone.utc)
        batch_id = uuid.uuid4()
        job_id = await _add_batch_job(db, sid, batch_id, export_dir, nbytes=100)
        cache = _make_batch_dir(export_dir, batch_id, 500)

        # 600 bytes on disk against a 200-byte cap: dropping the 500-byte cache
        # alone gets under it, so the export must survive.
        await purge_expired_exports(
            db, export_dir, retention_days=0, max_total_bytes=200, now=now
        )
        assert not os.path.isdir(cache)
        surviving = (await db.execute(select(ExportJob.id))).scalars().all()
        assert surviving == [job_id], "content was evicted before the regenerable cache"


@pytest.mark.asyncio
async def test_size_cap_still_evicts_exports_when_caches_are_not_enough(sessions, tmp_path):
    export_dir = str(tmp_path)
    async with sessions() as db:
        sid = await _source(db)
        now = datetime.now(timezone.utc)
        batch_id = uuid.uuid4()
        await _add_batch_job(db, sid, batch_id, export_dir, nbytes=400)
        cache = _make_batch_dir(export_dir, batch_id, 100)

        # 500 bytes against a 50-byte cap: the cache alone can't get us there.
        await purge_expired_exports(
            db, export_dir, retention_days=0, max_total_bytes=50, now=now
        )
        assert not os.path.isdir(cache)
        assert (await db.execute(select(ExportJob.id))).scalars().all() == []
