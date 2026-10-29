"""Job coverage resolution and target normalization."""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import DocumentationSource, Job, JobTarget, Product, Tier, Vendor
from app.services.job_coverage import normalize_targets, resolve_sources

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


async def _vendor(db, n_products=1, n_sources=1, **kw):
    sfx = uuid.uuid4().hex[:8]
    v = Vendor(name=f"V-{sfx}", **kw)
    db.add(v)
    await db.flush()
    prods = []
    for i in range(n_products):
        p = Product(vendor_id=v.id, name=f"P{i}-{sfx}")
        db.add(p)
        await db.flush()
        srcs = []
        for j in range(n_sources):
            s = DocumentationSource(product_id=p.id, name=f"S{j}", base_url=f"https://x/{sfx}/{i}/{j}")
            db.add(s)
            await db.flush()
            srcs.append(s)
        prods.append((p, srcs))
    return v, prods


async def _add_source(db, p):
    s = DocumentationSource(product_id=p.id, name="late", base_url=f"https://x/{uuid.uuid4().hex}")
    db.add(s)
    await db.flush()
    return s


async def test_tier_is_dynamic(factory):
    async with factory() as db:
        t = Tier(name="Gold", sort_order=1)
        db.add(t)
        await db.flush()
        v1, p1 = await _vendor(db, tier_id=t.id)
        job = Job(name="J", tier_id=t.id)
        db.add(job)
        await db.flush()
        assert (await resolve_sources(db, [job.id]))[job.id] == {s.id for _, ss in p1 for s in ss}
        v2, p2 = await _vendor(db, tier_id=t.id)
        got = (await resolve_sources(db, [job.id]))[job.id]
        assert got == {s.id for _, ss in p1 + p2 for s in ss}


async def test_vendor_and_product_targets_include_later_additions(factory):
    async with factory() as db:
        v, vp = await _vendor(db)
        v2, vp2 = await _vendor(db)
        job = Job(name="J")
        db.add(job)
        await db.flush()
        db.add(JobTarget(job_id=job.id, vendor_id=v.id))
        db.add(JobTarget(job_id=job.id, product_id=vp2[0][0].id))
        await db.flush()
        late1 = await _add_source(db, vp[0][0])
        late2 = await _add_source(db, vp2[0][0])
        got = (await resolve_sources(db, [job.id]))[job.id]
        assert late1.id in got and late2.id in got
        assert len(got) == 4


async def test_union_has_no_duplicates(factory):
    async with factory() as db:
        t = Tier(name="T", sort_order=0)
        db.add(t)
        await db.flush()
        v, vp = await _vendor(db, tier_id=t.id)
        p, ss = vp[0]
        job = Job(name="J", tier_id=t.id)
        db.add(job)
        await db.flush()
        db.add(JobTarget(job_id=job.id, vendor_id=v.id))
        db.add(JobTarget(job_id=job.id, product_id=p.id))
        db.add(JobTarget(job_id=job.id, source_id=ss[0].id))
        await db.flush()
        other = Job(name="Empty")
        db.add(other)
        await db.flush()
        res = await resolve_sources(db, [job.id, other.id])
        assert res[job.id] == {ss[0].id}
        assert res[other.id] == set()


async def test_normalize_full_product_and_full_vendor(factory):
    async with factory() as db:
        v, vp = await _vendor(db, n_products=2, n_sources=2)
        ids = [s.id for _, ss in vp for s in ss]
        t = await normalize_targets(db, ids)
        assert t.vendor_ids == [v.id] and t.product_ids == [] and t.source_ids == []


async def test_normalize_partial_product_keeps_sources(factory):
    async with factory() as db:
        v, vp = await _vendor(db, n_products=2, n_sources=2)
        full, partial = vp
        ids = [s.id for s in full[1]] + [partial[1][0].id]
        t = await normalize_targets(db, ids)
        assert t.vendor_ids == []
        assert t.product_ids == [full[0].id]
        assert t.source_ids == [partial[1][0].id]


async def test_product_with_no_sources_is_never_a_target(factory):
    async with factory() as db:
        v, vp = await _vendor(db, n_products=2, n_sources=1)
        empty = Product(vendor_id=v.id, name="Empty")
        db.add(empty)
        await db.flush()
        t = await normalize_targets(db, [vp[0][1][0].id])
        # The fully-picked product lifts to a product target; the empty one never
        # appears (and does not stop the lift).
        assert t.product_ids == [vp[0][0].id]
        assert t.vendor_ids == [] and t.source_ids == []
        assert empty.id not in t.product_ids
        assert (await normalize_targets(db, [])).product_ids == []


async def test_empty_product_does_not_block_vendor_target(factory):
    async with factory() as db:
        v, vp = await _vendor(db, n_products=2, n_sources=1)
        empty = Product(vendor_id=v.id, name="Empty")
        db.add(empty)
        await db.flush()
        t = await normalize_targets(db, [s.id for _, ss in vp for s in ss])
        assert t.vendor_ids == [v.id] and t.product_ids == [] and t.source_ids == []


async def test_vendor_with_only_empty_products_is_never_a_target(factory):
    async with factory() as db:
        v, _ = await _vendor(db, n_products=0)
        db.add(Product(vendor_id=v.id, name="Empty"))
        await db.flush()
        other, op = await _vendor(db, n_products=1, n_sources=1)
        t = await normalize_targets(db, [op[0][1][0].id])
        assert v.id not in t.vendor_ids and t.vendor_ids == [other.id]


async def test_deleted_target_disappears_and_resolution_still_works(factory):
    async with factory() as db:
        v, vp = await _vendor(db)
        v2, vp2 = await _vendor(db)
        job = Job(name="J")
        db.add(job)
        await db.flush()
        db.add(JobTarget(job_id=job.id, vendor_id=v.id))
        db.add(JobTarget(job_id=job.id, product_id=vp2[0][0].id))
        await db.commit()
        jid, vid = job.id, v.id
        keep = vp2[0][1][0].id
    async with factory() as db:
        await db.execute(delete(Vendor).where(Vendor.id == vid))
        await db.commit()
    async with factory() as db:
        assert (await resolve_sources(db, [jid]))[jid] == {keep}
