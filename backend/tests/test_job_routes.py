"""Tests for the Jobs API: CRUD, schedule fields, manual fan-out, run listing."""

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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, JobTarget
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
    # Unique vendor/product per call (vendors.name is UNIQUE; some tests create
    # several sources). Vendor name stays "Acme"-prefixed for the join assertion.
    sfx = uuid.uuid4().hex[:8]
    async with factory() as s:
        v = Vendor(name=f"Acme-{sfx}"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name=f"Cloud-{sfx}"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name=name, base_url=f"https://d/{sfx}/{name}")
        s.add(src); await s.commit()
        return src.id


async def _target(factory, job_id: str, source_id: uuid.UUID) -> None:
    async with factory() as s:
        s.add(JobTarget(job_id=uuid.UUID(job_id), source_id=source_id))
        await s.commit()


async def test_create_job_with_schedule_builds_cron(client):
    c, _ = client
    resp = await c.post("/api/jobs", json={
        "name": "Nightly", "enabled": True, "schedule_kind": "daily", "time_of_day": "03:30",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Nightly" and body["enabled"] is True
    assert body["cron"] == "30 3 * * *"
    assert body["next_run_at"] is not None


async def test_enabled_job_without_schedule_is_422(client):
    c, _ = client
    resp = await c.post("/api/jobs", json={"name": "Bad", "enabled": True})
    assert resp.status_code == 422


async def test_manual_group_job_has_no_cron(client):
    c, _ = client
    body = (await c.post("/api/jobs", json={"name": "Group"})).json()
    assert body["enabled"] is False and body["cron"] is None and body["next_run_at"] is None


async def test_delete_job_keeps_sources_and_drops_targets(client):
    c, factory = client
    sid = await _source(factory)
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    await _target(factory, job["id"], sid)

    assert (await c.delete(f"/api/jobs/{job['id']}")).status_code == 204
    async with factory() as s:
        assert await s.get(DocumentationSource, sid) is not None
        assert (await s.execute(select(JobTarget))).scalars().all() == []


async def test_run_job_fans_out_to_pending_runs(client):
    c, factory = client
    s1 = await _source(factory, "A")
    s2 = await _source(factory, "B")
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    await _target(factory, job["id"], s1)
    await _target(factory, job["id"], s2)

    jr = (await c.post(f"/api/jobs/{job['id']}/run")).json()
    assert jr["trigger"] == "manual" and jr["sources_total"] == 2

    async with factory() as s:
        runs = (await s.execute(select(ExtractionRun))).scalars().all()
        assert len(runs) == 2
        assert all(r.status == RunStatus.PENDING and str(r.job_run_id) == jr["id"] for r in runs)

    listed = (await c.get(f"/api/jobs/{job['id']}/runs")).json()
    assert len(listed) == 1 and listed[0]["id"] == jr["id"]


async def test_recent_job_runs_across_all_jobs_tagged_with_name(client):
    c, factory = client
    s1 = await _source(factory, "A")
    job = (await c.post("/api/jobs", json={"name": "Nightly"})).json()
    await _target(factory, job["id"], s1)
    jr = (await c.post(f"/api/jobs/{job['id']}/run")).json()

    # The literal /runs path must not be captured by /{job_id}.
    resp = await c.get("/api/jobs/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == jr["id"]
    assert body[0]["job_name"] == "Nightly"


async def test_run_job_without_sources_is_409(client):
    c, _ = client
    job = (await c.post("/api/jobs", json={"name": "Empty"})).json()
    assert (await c.post(f"/api/jobs/{job['id']}/run")).status_code == 409


async def test_update_job_recomputes_schedule(client):
    c, _ = client
    job = (await c.post("/api/jobs", json={"name": "J"})).json()
    updated = (await c.patch(f"/api/jobs/{job['id']}", json={
        "enabled": True, "schedule_kind": "weekly", "time_of_day": "04:00", "weekday": 1,
    })).json()
    assert updated["cron"] == "0 4 * * 1" and updated["next_run_at"] is not None
