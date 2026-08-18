"""Tests for the refactored trigger route (enqueue-based).

The trigger route now enqueues a pending run via enqueue_run instead of
running extraction in-process via BackgroundTask.
"""

import os
import sys

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
from app.models import Vendor, Product, DocumentationSource

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def client():
    """Yield (AsyncClient, session_factory).

    Per-test NullPool engine so connections bind to this test's event loop.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_trigger_enqueues_pending_run(client):
    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V"); db.add(v); await db.flush()
        s_prod = Product(vendor_id=v.id, name="P")
        db.add(s_prod)
        await db.flush()
        s = DocumentationSource(product_id=s_prod.id, name="S", base_url="http://x")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r1 = await c.post(f"/api/extraction/trigger/{sid}")
    assert r1.status_code == 200
    assert r1.json()["status"] == "pending"

    # Second trigger while one is active -> 409 (coalesced by the DB invariant).
    r2 = await c.post(f"/api/extraction/trigger/{sid}")
    assert r2.status_code == 409

    runs = await c.get(f"/api/extraction/runs?source_id={sid}")
    assert runs.json()["runs"][0]["trigger"] == "manual"


async def test_trigger_force_sets_force_trigger(client):
    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V2"); db.add(v); await db.flush()
        prod = Product(vendor_id=v.id, name="P2"); db.add(prod); await db.flush()
        s = DocumentationSource(product_id=prod.id, name="S2", base_url="http://x2")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r = await c.post(f"/api/extraction/trigger/{sid}?force=true")
    assert r.status_code == 200 and r.json()["status"] == "pending"

    runs = await c.get(f"/api/extraction/runs?source_id={sid}")
    assert runs.json()["runs"][0]["trigger"] == "force"


async def test_trigger_allow_toc_collapse_rides_on_the_run(client):
    # The TOC-collapse override is per-run (not a global setting), so the flag must
    # reach the queued row the worker will claim — and default off, so an ordinary
    # or scheduled trigger stays protected.
    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V3"); db.add(v); await db.flush()
        prod = Product(vendor_id=v.id, name="P3"); db.add(prod); await db.flush()
        s = DocumentationSource(product_id=prod.id, name="S3", base_url="http://x3")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    r = await c.post(f"/api/extraction/trigger/{sid}?allow_toc_collapse=true")
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    detail = await c.get(f"/api/extraction/runs/{run_id}")
    assert detail.json()["allow_toc_collapse"] is True
    listed = await c.get(f"/api/extraction/runs?source_id={sid}")
    assert listed.json()["runs"][0]["allow_toc_collapse"] is True


async def test_plain_trigger_does_not_override_the_collapse_guard(client):
    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V4"); db.add(v); await db.flush()
        prod = Product(vendor_id=v.id, name="P4"); db.add(prod); await db.flush()
        s = DocumentationSource(product_id=prod.id, name="S4", base_url="http://x4")
        db.add(s); await db.commit(); await db.refresh(s)
        sid = str(s.id)

    # force=true is about the unchanged-content fast path, NOT the data-loss guard:
    # the two must stay independent.
    r = await c.post(f"/api/extraction/trigger/{sid}?force=true")
    assert r.status_code == 200
    listed = await c.get(f"/api/extraction/runs?source_id={sid}")
    assert listed.json()["runs"][0]["allow_toc_collapse"] is False


async def test_run_listing_flags_a_collapse_failure_as_overridable(client):
    # The UI only offers "Extract anyway" for this specific failure, so the flag
    # must be true for a guard failure and false for any other failed run.
    from app.models import ExtractionRun
    from app.models.extraction_run import RunStatus
    from app.services.firecrawl import TOC_COLLAPSE_PREFIX

    c, session_factory = client
    async with session_factory() as db:
        v = Vendor(name="V5"); db.add(v); await db.flush()
        prod = Product(vendor_id=v.id, name="P5"); db.add(prod); await db.flush()
        s = DocumentationSource(product_id=prod.id, name="S5", base_url="http://x5")
        db.add(s); await db.flush()
        collapsed = ExtractionRun(
            source_id=s.id, status=RunStatus.FAILED,
            error_message=f"{TOC_COLLAPSE_PREFIX} for 'S5': found 255 …",
        )
        other = ExtractionRun(
            source_id=s.id, status=RunStatus.FAILED,
            error_message="Firecrawl unavailable",
        )
        db.add_all([collapsed, other]); await db.commit()
        sid, collapsed_id, other_id = str(s.id), str(collapsed.id), str(other.id)

    listed = (await c.get(f"/api/extraction/runs?source_id={sid}")).json()["runs"]
    flags = {r["id"]: r["toc_collapsed"] for r in listed}
    assert flags[collapsed_id] is True
    assert flags[other_id] is False
    detail = await c.get(f"/api/extraction/runs/{collapsed_id}")
    assert detail.json()["toc_collapsed"] is True
