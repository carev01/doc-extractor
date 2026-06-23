"""Tests for the Jobs API: CRUD, source assignment, manual fan-out, run listing."""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, ExtractionRun
from app.models.extraction_run import RunStatus

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(factory, name="Docs") -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name=name, base_url="https://d/" + name)
        s.add(src); await s.commit()
        return src.id


async def test_create_job_with_schedule_builds_cron(client):
    c, _ = client
    resp = await c.post("/api/jobs", json={
        "name": "Nightly", "enabled": True, "frequency": "daily", "time_of_day": "03:30",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Nightly" and body["enabled"] is True
    assert body["cron"] == "30 3 * * *"
    assert body["next_run_at"] is not None
    assert body["source_count"] == 0


async def test_enabled_job_without_frequency_is_422(client):
    c, _ = client
    resp = await c.post("/api/jobs", json={"name": "Bad", "enabled": True})
    assert resp.status_code == 422


async def test_manual_group_job_has_no_cron(client):
    c, _ = client
    body = (await c.post("/api/jobs", json={"name": "Group"})).json()
    assert body["enabled"] is False and body["cron"] is None and body["next_run_at"] is None


async def test_assign_and_unassign_source(client):
    c, factory = client
    sid = await _source(factory)
    job = (await c.post("/api/jobs", json={"name": "J"})).json()

    assigned = (await c.put(f"/api/jobs/{job['id']}/sources/{sid}")).json()
    assert assigned["source_count"] == 1
    assert assigned["sources"][0]["id"] == str(sid)
    assert assigned["sources"][0]["vendor_name"] == "Acme"

    unassigned = (await c.delete(f"/api/jobs/{job['id']}/sources/{sid}")).json()
    assert unassigned["source_count"] == 0


async def test_one_job_per_source_reassigns(client):
    c, factory = client
    sid = await _source(factory)
    j1 = (await c.post("/api/jobs", json={"name": "J1"})).json()
    j2 = (await c.post("/api/jobs", json={"name": "J2"})).json()
    await c.put(f"/api/jobs/{j1['id']}/sources/{sid}")
    await c.put(f"/api/jobs/{j2['id']}/sources/{sid}")   # reassign to J2

    assert (await c.get(f"/api/jobs/{j1['id']}")).json()["source_count"] == 0
    assert (await c.get(f"/api/jobs/{j2['id']}")).json()["source_count"] == 1


async def test_delete_job_unassigns_sources(client):
    c, factory = client
    sid = await _source(factory)
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    await c.put(f"/api/jobs/{job['id']}/sources/{sid}")

    assert (await c.delete(f"/api/jobs/{job['id']}")).status_code == 204
    # source survives, now unassigned
    async with factory() as s:
        src = await s.get(DocumentationSource, sid)
        assert src is not None and src.job_id is None


async def test_run_job_fans_out_to_pending_runs(client):
    c, factory = client
    s1 = await _source(factory, "A")
    s2 = await _source(factory, "B")
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    await c.put(f"/api/jobs/{job['id']}/sources/{s1}")
    await c.put(f"/api/jobs/{job['id']}/sources/{s2}")

    jr = (await c.post(f"/api/jobs/{job['id']}/run")).json()
    assert jr["trigger"] == "manual" and jr["sources_total"] == 2

    async with factory() as s:
        runs = (await s.execute(select(ExtractionRun))).scalars().all()
        assert len(runs) == 2
        assert all(r.status == RunStatus.PENDING and str(r.job_run_id) == jr["id"] for r in runs)

    listed = (await c.get(f"/api/jobs/{job['id']}/runs")).json()
    assert len(listed) == 1 and listed[0]["id"] == jr["id"]


async def test_run_job_without_sources_is_409(client):
    c, _ = client
    job = (await c.post("/api/jobs", json={"name": "Empty"})).json()
    assert (await c.post(f"/api/jobs/{job['id']}/run")).status_code == 409


async def test_update_job_recomputes_schedule(client):
    c, _ = client
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    updated = (await c.patch(f"/api/jobs/{job['id']}", json={
        "enabled": True, "frequency": "weekly", "time_of_day": "04:00", "day_of_week": 1,
    })).json()
    assert updated["cron"] == "0 4 * * 1" and updated["next_run_at"] is not None
