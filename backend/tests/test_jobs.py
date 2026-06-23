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
    Vendor, Product, DocumentationSource, ExtractionRun, Schedule,
)
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
