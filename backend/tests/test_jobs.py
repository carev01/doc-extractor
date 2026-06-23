"""Tests for the Jobs-view endpoints: enriched run list, run logs, schedule list."""

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import (
    Vendor, Product, DocumentationSource, ExtractionRun, Schedule, ExportJob,
)
from app.models.extraction_run import RunStatus
from app.models.export_job import ExportStatus

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


async def _source(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Docs", base_url="https://d.acme.com")
        s.add(src); await s.commit()
        return src.id


async def test_list_runs_includes_vendor_product_source_names(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        s.add(ExtractionRun(source_id=sid, status=RunStatus.COMPLETED,
                            articles_extracted=5, articles_total=5))
        await s.commit()

    body = (await c.get("/api/extraction/runs")).json()
    assert len(body["runs"]) == 1
    r = body["runs"][0]
    assert r["vendor_name"] == "Acme"
    assert r["product_name"] == "Cloud"
    assert r["source_name"] == "Docs"
    assert r["articles_extracted"] == 5


async def test_list_runs_status_filter(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        s.add(ExtractionRun(source_id=sid, status=RunStatus.FAILED, error_message="boom"))
        await s.commit()
    ok = (await c.get("/api/extraction/runs", params={"status": "failed"})).json()
    assert len(ok["runs"]) == 1 and ok["runs"][0]["status"] == "failed"
    none = (await c.get("/api/extraction/runs", params={"status": "completed"})).json()
    assert none["runs"] == []


async def test_run_logs_endpoint(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        run = ExtractionRun(source_id=sid, status=RunStatus.RUNNING, log_text="line1\nline2\n")
        s.add(run); await s.commit()
        rid = run.id

    body = (await c.get(f"/api/extraction/runs/{rid}/logs")).json()
    assert body["log_text"] == "line1\nline2\n"
    # unknown run → 404
    assert (await c.get(f"/api/extraction/runs/{uuid.uuid4()}/logs")).status_code == 404


async def test_list_schedules_with_names(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        s.add(Schedule(
            source_id=sid, enabled=True, frequency="daily", time_of_day="02:00",
            cron="0 2 * * *", timezone="UTC",
            next_run_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ))
        await s.commit()

    body = (await c.get("/api/schedules")).json()
    assert len(body["schedules"]) == 1
    sch = body["schedules"][0]
    assert sch["vendor_name"] == "Acme"
    assert sch["product_name"] == "Cloud"
    assert sch["source_name"] == "Docs"
    assert sch["enabled"] is True
    assert sch["next_run_at"].startswith("2026-07-01")

    # enabled_only filter
    empty = (await c.get("/api/schedules", params={"enabled_only": True})).json()
    assert len(empty["schedules"]) == 1


async def test_list_export_jobs_with_names(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        s.add(ExportJob(source_id=sid, status=ExportStatus.PENDING,
                        request={"source_id": str(sid), "format": "pdf"}))
        await s.commit()
    body = (await c.get("/api/export/jobs")).json()
    assert len(body["jobs"]) == 1
    j = body["jobs"][0]
    assert j["vendor_name"] == "Acme" and j["source_name"] == "Docs"
    assert j["format"] == "pdf" and j["status"] == "pending"


async def test_cancel_queued_export_job(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        job = ExportJob(source_id=sid, status=ExportStatus.PENDING,
                        request={"source_id": str(sid), "format": "markdown"})
        s.add(job); await s.commit()
        jid = job.id

    resp = await c.post(f"/api/export/jobs/{jid}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"


async def test_cannot_cancel_running_export_job(client):
    c, factory = client
    sid = await _source(factory)
    async with factory() as s:
        job = ExportJob(source_id=sid, status=ExportStatus.RUNNING,
                        request={"source_id": str(sid), "format": "markdown"})
        s.add(job); await s.commit()
        jid = job.id

    resp = await c.post(f"/api/export/jobs/{jid}/cancel")
    assert resp.status_code == 409


# ── Run cancel / pause / resume ──

async def _run(factory, sid, status):
    async with factory() as s:
        run = ExtractionRun(source_id=sid, status=status)
        s.add(run); await s.commit()
        return run.id


async def test_cancel_pending_run_ends_immediately(client):
    c, factory = client
    sid = await _source(factory)
    rid = await _run(factory, sid, RunStatus.PENDING)
    resp = await c.post(f"/api/extraction/runs/{rid}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"


async def test_cancel_running_run_sets_control_flag(client):
    c, factory = client
    sid = await _source(factory)
    rid = await _run(factory, sid, RunStatus.RUNNING)
    resp = await c.post(f"/api/extraction/runs/{rid}/cancel")
    assert resp.status_code == 200
    # Still RUNNING; worker honours the flag at the next batch boundary.
    body = (await c.get(f"/api/extraction/runs/{rid}")).json()
    assert body["status"] == "running" and body["control"] == "cancel"


async def test_pause_running_then_resume(client):
    c, factory = client
    sid = await _source(factory)
    rid = await _run(factory, sid, RunStatus.RUNNING)
    # pause → control flag set
    assert (await c.post(f"/api/extraction/runs/{rid}/pause")).status_code == 200
    assert (await c.get(f"/api/extraction/runs/{rid}")).json()["control"] == "pause"
    # simulate the worker having paused the run
    async with factory() as s:
        run = await s.get(ExtractionRun, rid)
        run.status = RunStatus.PAUSED; run.control = None
        await s.commit()
    # resume → back to pending for re-claim
    resp = await c.post(f"/api/extraction/runs/{rid}/resume")
    assert resp.status_code == 200 and resp.json()["status"] == "pending"


async def test_pause_pending_run_holds_it(client):
    c, factory = client
    sid = await _source(factory)
    rid = await _run(factory, sid, RunStatus.PENDING)
    resp = await c.post(f"/api/extraction/runs/{rid}/pause")
    assert resp.status_code == 200 and resp.json()["status"] == "paused"


async def test_resume_non_paused_is_409(client):
    c, factory = client
    sid = await _source(factory)
    rid = await _run(factory, sid, RunStatus.RUNNING)
    assert (await c.post(f"/api/extraction/runs/{rid}/resume")).status_code == 409


async def test_cancel_completed_run_is_409(client):
    c, factory = client
    sid = await _source(factory)
    rid = await _run(factory, sid, RunStatus.COMPLETED)
    assert (await c.post(f"/api/extraction/runs/{rid}/cancel")).status_code == 409
