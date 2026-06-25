"""GET /api/dashboard/sources returns summary + per-source health rows."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.source import SourceStatus
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


async def test_dashboard_summary_and_rows(client):
    c, factory = client
    now = datetime.now(timezone.utc)
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()

        fresh = DocumentationSource(
            product_id=p.id, name="Fresh", base_url="https://d/1",
            status=SourceStatus.COMPLETED, last_extracted_at=now - timedelta(days=1),
        )
        stale = DocumentationSource(
            product_id=p.id, name="Stale", base_url="https://d/2",
            status=SourceStatus.COMPLETED, last_extracted_at=now - timedelta(days=40),
        )
        never = DocumentationSource(
            product_id=p.id, name="Never", base_url="https://d/3",
            status=SourceStatus.PENDING,
        )
        failed = DocumentationSource(
            product_id=p.id, name="Failed", base_url="https://d/4",
            status=SourceStatus.FAILED, last_extracted_at=now - timedelta(days=2),
        )
        s.add_all([fresh, stale, never, failed]); await s.flush()

        run = ExtractionRun(
            source_id=fresh.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=1),
            articles_extracted=3, articles_updated=1, articles_unchanged=5,
        )
        s.add(run); await s.flush()
        # one active + one removed article on fresh
        s.add(Article(source_id=fresh.id, title="A", source_url="https://d/1/a",
                      topic_key="a", content_markdown="x"))
        s.add(Article(source_id=fresh.id, title="B", source_url="https://d/1/b",
                      topic_key="b", content_markdown="x",
                      removed_at=now - timedelta(days=1)))
        await s.commit()

    body = (await c.get("/api/dashboard/sources?stale_days=30")).json()
    summary = body["summary"]
    assert summary["total"] == 4
    assert summary["never_extracted"] == 1
    assert summary["stale"] == 1      # Stale only; Never is not stale
    assert summary["failing"] == 1

    rows = {r["name"]: r for r in body["sources"]}
    assert rows["Never"]["age_seconds"] is None
    assert rows["Fresh"]["article_count"] == 1   # removed article excluded
    assert rows["Fresh"]["last_run_status"] == "completed"
    assert rows["Fresh"]["last_run_new"] == 3
    assert rows["Fresh"]["vendor_name"] == "Acme"


async def test_pending_run_does_not_shadow_completed(client):
    """A PENDING run (started_at auto-set by DB) must not shadow a COMPLETED run.

    ExtractionRun.started_at has server_default=now() so it is never NULL even
    for PENDING rows. The dashboard must use status priority ordering so that a
    newer PENDING run (started_at ≈ now) does not hide the meaningful stats of
    an older COMPLETED run (articles_extracted > 0).
    """
    c, factory = client
    now = datetime.now(timezone.utc)
    async with factory() as s:
        v = Vendor(name="VendorZ"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="ProdZ"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="SrcZ", base_url="https://z/1",
            status=SourceStatus.COMPLETED,
            last_extracted_at=now - timedelta(days=1),
        )
        s.add(src); await s.flush()
        completed = ExtractionRun(
            source_id=src.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=1),
            articles_extracted=4, articles_updated=0, articles_unchanged=0,
        )
        # PENDING has a more recent started_at (DB server_default=now()) which
        # would incorrectly win under naive started_at DESC ordering.
        pending = ExtractionRun(
            source_id=src.id, status=RunStatus.PENDING,
            started_at=now,           # explicitly set to now to simulate server default
            articles_extracted=0, articles_updated=0, articles_unchanged=0,
        )
        s.add_all([completed, pending])
        await s.commit()

    body = (await c.get("/api/dashboard/sources?stale_days=30")).json()
    rows = {r["name"]: r for r in body["sources"]}
    assert rows["SrcZ"]["last_run_status"] == "completed"
    assert rows["SrcZ"]["last_run_new"] == 4


async def test_summary_running_counter(client):
    """summary.running reflects EXTRACTING sources."""
    c, factory = client
    now = datetime.now(timezone.utc)
    async with factory() as s:
        v = Vendor(name="VendorR"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="ProdR"); s.add(p); await s.flush()
        extracting = DocumentationSource(
            product_id=p.id, name="SrcR", base_url="https://r/1",
            status=SourceStatus.EXTRACTING,
            last_extracted_at=now - timedelta(hours=1),
        )
        s.add(extracting)
        await s.commit()

    body = (await c.get("/api/dashboard/sources?stale_days=30")).json()
    summary = body["summary"]
    assert summary["running"] == 1
    assert summary["total"] == 1
