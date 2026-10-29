"""Tier exclusions for jobs: coverage, API, cascades, schedule view, fan-out."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.authz import Principal, get_principal
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import (
    DocumentationSource, ExtractionRun, Job, JobExclusion, JobTarget, Product, Tier, Vendor,
)
from app.models.user import UserRole
from app.services.job_coverage import resolve_sources
from app.services.scheduling import fan_out_job

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio
ADMIN = Principal(see_all=True, role=UserRole.ADMIN)


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    state = {"principal": ADMIN}

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_principal] = lambda: state["principal"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as cl:
        yield cl, factory, state
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _world(s):
    """Gold tier with vendors A (excluded in tests) and B; vendor X in no tier."""
    gold = Tier(name="Gold", sort_order=1)
    s.add(gold)
    await s.flush()
    out = {"tier": gold}
    for key, tier in (("A", gold.id), ("B", gold.id), ("X", None)):
        v = Vendor(name=f"V{key}", tier_id=tier)
        s.add(v)
        await s.flush()
        p = Product(vendor_id=v.id, name=f"P{key}")
        s.add(p)
        await s.flush()
        src = DocumentationSource(product_id=p.id, name=f"S{key}", base_url=f"http://{key}")
        s.add(src)
        await s.flush()
        out[key] = (v, p, src)
    return out


async def _tier_job(s, w, **kw):
    j = Job(name="J", tier_id=w["tier"].id, **kw)
    s.add(j)
    await s.flush()
    return j


async def test_resolve_drops_excluded_tier_vendor(env):
    _, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        assert (await resolve_sources(s, [j.id]))[j.id] == {w["A"][2].id, w["B"][2].id}
        s.add(JobExclusion(job_id=j.id, vendor_id=w["A"][0].id))
        await s.flush()
        assert (await resolve_sources(s, [j.id]))[j.id] == {w["B"][2].id}


async def test_explicit_pick_of_excluded_vendor_still_covered(env):
    _, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        s.add(JobExclusion(job_id=j.id, vendor_id=w["A"][0].id))
        s.add(JobTarget(job_id=j.id, product_id=w["A"][1].id))
        await s.flush()
        assert (await resolve_sources(s, [j.id]))[j.id] == {w["A"][2].id, w["B"][2].id}


async def test_exclusion_of_vendor_outside_tier_is_inert(env):
    _, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        s.add(JobExclusion(job_id=j.id, vendor_id=w["X"][0].id))
        await s.flush()
        assert (await resolve_sources(s, [j.id]))[j.id] == {w["A"][2].id, w["B"][2].id}


async def test_put_exclusions_replaces_and_reports(env):
    c, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        await s.commit()
        a, b, x = (str(w[k][0].id) for k in "ABX")
    jid = str(j.id)
    r = await c.get(f"/api/jobs/{jid}")
    assert r.json()["excluded_vendor_ids"] == [] and r.json()["coverage"]["excluded_vendors"] == 0
    assert r.json()["coverage"]["sources"] == 2
    r = await c.put(f"/api/jobs/{jid}/exclusions", json={"vendor_ids": [a, x]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["excluded_vendor_ids"]) == sorted([a, x])
    assert body["coverage"]["excluded_vendors"] == 1      # X is not in the tier
    assert body["coverage"]["sources"] == 1
    r = await c.put(f"/api/jobs/{jid}/exclusions", json={"vendor_ids": [b, b]})
    assert r.json()["excluded_vendor_ids"] == [b] and r.json()["coverage"]["sources"] == 1
    r = await c.put(f"/api/jobs/{jid}/exclusions", json={"vendor_ids": []})
    assert r.json()["excluded_vendor_ids"] == [] and r.json()["coverage"]["sources"] == 2
    listed = (await c.get("/api/jobs")).json()["jobs"][0]
    assert "excluded_vendor_ids" in listed


async def test_put_exclusions_unknown_vendor_404_and_keeps_set(env):
    c, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        await s.commit()
        a = str(w["A"][0].id)
    await c.put(f"/api/jobs/{j.id}/exclusions", json={"vendor_ids": [a]})
    bad = str(uuid.uuid4())
    r = await c.put(f"/api/jobs/{j.id}/exclusions", json={"vendor_ids": [bad]})
    assert r.status_code == 404 and bad in r.json()["detail"]
    assert (await c.get(f"/api/jobs/{j.id}")).json()["excluded_vendor_ids"] == [a]
    r = await c.put(f"/api/jobs/{uuid.uuid4()}/exclusions", json={"vendor_ids": []})
    assert r.status_code == 404


async def test_exclusions_admin_only(env):
    c, f, state = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        await s.commit()
    state["principal"] = Principal(see_all=True, role=UserRole.READ_WRITE)
    r = await c.put(f"/api/jobs/{j.id}/exclusions", json={"vendor_ids": []})
    assert r.status_code == 403


async def _count(f):
    async with f() as s:
        return (await s.execute(select(func.count()).select_from(JobExclusion))).scalar_one()


async def test_cascade_on_vendor_and_job_delete(env):
    c, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        s.add_all([JobExclusion(job_id=j.id, vendor_id=w["A"][0].id),
                   JobExclusion(job_id=j.id, vendor_id=w["B"][0].id)])
        await s.commit()
    assert await _count(f) == 2
    async with f() as s:
        await s.execute(delete(Vendor).where(Vendor.id == w["A"][0].id))
        await s.commit()
    assert await _count(f) == 1
    r = await c.delete(f"/api/jobs/{j.id}")
    assert r.status_code == 204
    assert await _count(f) == 0


async def test_schedule_view_excluded_vendor_not_scheduled(env):
    c, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w, enabled=True,
                            next_run_at=datetime.now(timezone.utc) + timedelta(hours=3))
        s.add(JobExclusion(job_id=j.id, vendor_id=w["A"][0].id))
        await s.commit()
    prods = {p["product_name"]: p
             for p in (await c.get("/api/jobs/schedule")).json()["products"]}
    assert prods["PA"]["scheduled_sources"] == 0 and prods["PA"]["next_at"] is None
    assert prods["PB"]["scheduled_sources"] == 1 and prods["PB"]["next_at"] is not None


async def test_fan_out_skips_excluded_vendor(env):
    _, f, _ = env
    async with f() as s:
        w = await _world(s)
        j = await _tier_job(s, w)
        s.add(JobExclusion(job_id=j.id, vendor_id=w["A"][0].id))
        await s.flush()
        jr = await fan_out_job(s, j, "manual")
        runs = (await s.execute(
            select(ExtractionRun.source_id).where(ExtractionRun.job_run_id == jr.id)
        )).scalars().all()
        assert set(runs) == {w["B"][2].id}
