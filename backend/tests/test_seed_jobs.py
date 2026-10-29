"""Seed script: tiers + three disabled, staggered tier jobs."""
import os
import sys
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor
from app.models.job import Job
from app.models.job_run import JobRun
from app.models.job_target import JobTarget
from app.models.tier import Tier
from app.ops.seed_jobs import seed
from app.services.cron import job_spec, spec_to_cron

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_seed_creates_three_disabled_staggered_jobs(factory):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)  # next month = 11
    async with factory() as db:
        await seed(db, now=now)
        await db.commit()
        tiers = {t.name: t for t in (await db.execute(select(Tier))).scalars()}
        assert {n: t.sort_order for n, t in tiers.items()} == {"Tier 1": 1, "Tier 2": 2, "Tier 3": 3}
        jobs = {j.name: j for j in (await db.execute(select(Job))).scalars()}
        assert set(jobs) == {"Tier 1", "Tier 2", "Tier 3"}
        expect = {  # nth, weekday, interval_months, cron
            "Tier 1": ("first", 0, 1, "0 2 * * 0#1"),
            "Tier 2": ("first", 6, 2, "0 2 * 1,3,5,7,9,11 6#1"),
            "Tier 3": ("last", 6, 3, "0 2 * 2,5,8,11 L6"),
        }
        for name, (nth, wd, interval, cron) in expect.items():
            j = jobs[name]
            assert (j.schedule_kind, j.interval_months, j.nth, j.weekday) == ("monthly_nth", interval, nth, wd)
            assert (j.time_of_day, j.timezone, j.enabled) == ("02:00", "UTC", False)
            assert j.start_month == 11
            assert j.tier_id == tiers[name].id
            assert j.next_run_at is None
            assert j.cron == cron == spec_to_cron(job_spec(j))


async def test_seed_start_month_wraps_december(factory):
    async with factory() as db:
        await seed(db, now=datetime(2026, 12, 15, tzinfo=timezone.utc))
        assert {j.start_month for j in (await db.execute(select(Job))).scalars()} == {1}


async def test_seed_is_idempotent(factory):
    async with factory() as db:
        await seed(db)
        await db.commit()
        await seed(db)
        await db.commit()
        assert (await db.execute(select(func.count()).select_from(Tier))).scalar_one() == 3
        assert (await db.execute(select(func.count()).select_from(Job))).scalar_one() == 3


async def test_major_competitors_deleted_only_when_unused(factory):
    async with factory() as db:
        empty = Job(name="Major competitors", enabled=False)
        db.add(empty)
        await db.commit()
        res = await seed(db)
        await db.commit()
        assert res["deleted_major_competitors"] is True
        assert (await db.execute(select(Job).where(Job.name == "Major competitors"))).first() is None

    async with factory() as db:
        v = Vendor(name="V")
        db.add(v)
        await db.flush()
        j = Job(name="Major competitors", enabled=False)
        db.add(j)
        await db.flush()
        db.add(JobTarget(job_id=j.id, vendor_id=v.id))
        await db.commit()
        res = await seed(db)
        await db.commit()
        assert res["deleted_major_competitors"] is False
        assert (await db.execute(select(Job).where(Job.name == "Major competitors"))).first() is not None

    async with factory() as db:
        j = (await db.execute(select(Job).where(Job.name == "Major competitors"))).scalar_one()
        await db.delete(j)
        j2 = Job(name="Major competitors", enabled=False)
        db.add(j2)
        await db.flush()
        db.add(JobRun(job_id=j2.id))
        await db.commit()
        res = await seed(db)
        await db.commit()
        assert res["deleted_major_competitors"] is False
