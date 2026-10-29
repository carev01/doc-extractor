"""Data model for the jobs overhaul: tiers, job targets, schedule fields."""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Job, JobTarget, Product, Tier, Vendor
from app.services.cron import job_spec, spec_to_cron

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


async def _vendor_product(db):
    sfx = uuid.uuid4().hex[:8]
    v = Vendor(name=f"V-{sfx}")
    db.add(v)
    await db.flush()
    p = Product(vendor_id=v.id, name=f"P-{sfx}")
    db.add(p)
    await db.flush()
    return v, p


async def test_target_requires_exactly_one_ref(factory):
    async with factory() as db:
        v, p = await _vendor_product(db)
        job = Job(name="J")
        db.add(job)
        await db.flush()
        db.add(JobTarget(job_id=job.id, vendor_id=v.id, product_id=p.id))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
    async with factory() as db:
        job = Job(name="J2")
        db.add(job)
        await db.flush()
        db.add(JobTarget(job_id=job.id))
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_deleting_a_product_drops_its_target_only(factory):
    async with factory() as db:
        v, p = await _vendor_product(db)
        job = Job(name="J")
        db.add(job)
        await db.flush()
        db.add(JobTarget(job_id=job.id, vendor_id=v.id))
        db.add(JobTarget(job_id=job.id, product_id=p.id))
        await db.commit()
        jid, pid = job.id, p.id
    async with factory() as db:
        await db.delete(await db.get(Product, pid))
        await db.commit()
    async with factory() as db:
        rows = (await db.execute(select(JobTarget).where(JobTarget.job_id == jid))).scalars().all()
        assert len(rows) == 1 and rows[0].vendor_id is not None
        assert await db.get(Job, jid) is not None


async def test_tier_in_use_cannot_be_deleted_at_db_level(factory):
    async with factory() as db:
        t = Tier(name="Gold", sort_order=1)
        db.add(t)
        await db.flush()
        db.add(Vendor(name="TV", tier_id=t.id))
        await db.commit()
        tid = t.id
    async with factory() as db:
        with pytest.raises(IntegrityError):
            await db.delete(await db.get(Tier, tid))
            await db.commit()


async def test_job_spec_roundtrip(factory):
    async with factory() as db:
        job = Job(
            name="Q", enabled=True, schedule_kind="monthly_nth", interval_months=3,
            start_month=2, nth="last", weekday=5, time_of_day="04:15", timezone="UTC",
        )
        job.cron = "15 4 * 2,5,8,11 L5"
        db.add(job)
        await db.commit()
        await db.refresh(job)
        assert spec_to_cron(job_spec(job)) == job.cron


async def test_deleting_a_vendor_drops_its_targets_and_its_products_targets(factory):
    from app.models import DocumentationSource
    async with factory() as db:
        v, p = await _vendor_product(db)
        other_v, _ = await _vendor_product(db)
        s = DocumentationSource(product_id=p.id, name="S", base_url="https://x/s")
        db.add(s)
        job = Job(name="J")
        db.add(job)
        await db.flush()
        db.add_all([
            JobTarget(job_id=job.id, vendor_id=v.id),
            JobTarget(job_id=job.id, product_id=p.id),
            JobTarget(job_id=job.id, source_id=s.id),
            JobTarget(job_id=job.id, vendor_id=other_v.id),
        ])
        await db.commit()
        jid, vid, ovid = job.id, v.id, other_v.id
    async with factory() as db:
        await db.delete(await db.get(Vendor, vid))
        await db.commit()
    async with factory() as db:
        rows = (await db.execute(select(JobTarget).where(JobTarget.job_id == jid))).scalars().all()
        assert [(r.vendor_id, r.product_id, r.source_id) for r in rows] == [(ovid, None, None)]
        assert await db.get(Job, jid) is not None


async def test_deleting_a_source_drops_its_target_only(factory):
    from app.models import DocumentationSource
    async with factory() as db:
        v, p = await _vendor_product(db)
        s1 = DocumentationSource(product_id=p.id, name="S1", base_url="https://x/1")
        s2 = DocumentationSource(product_id=p.id, name="S2", base_url="https://x/2")
        db.add_all([s1, s2])
        job = Job(name="J")
        db.add(job)
        await db.flush()
        db.add_all([JobTarget(job_id=job.id, source_id=s1.id),
                    JobTarget(job_id=job.id, source_id=s2.id)])
        await db.commit()
        jid, s1id, s2id = job.id, s1.id, s2.id
    async with factory() as db:
        await db.delete(await db.get(DocumentationSource, s1id))
        await db.commit()
    async with factory() as db:
        rows = (await db.execute(select(JobTarget).where(JobTarget.job_id == jid))).scalars().all()
        assert [r.source_id for r in rows] == [s2id]


async def test_tier_used_by_a_job_cannot_be_deleted_at_db_level(factory):
    """Job.tier_id is ON DELETE RESTRICT, independently of vendors using it."""
    async with factory() as db:
        t = Tier(name="Gold", sort_order=1)
        db.add(t)
        await db.flush()
        db.add(Job(name="J", tier_id=t.id))
        await db.commit()
        tid = t.id
    async with factory() as db:
        with pytest.raises(IntegrityError):
            await db.delete(await db.get(Tier, tid))
            await db.commit()
    async with factory() as db:
        assert await db.get(Tier, tid) is not None
