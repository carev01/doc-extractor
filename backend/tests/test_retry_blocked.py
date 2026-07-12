"""Blocked-page retry: pure threshold/dedup logic + the manual retry route.

The auto-retry *pass* itself (a second scrape) is exercised end-to-end by the
extraction integration tests with a stubbed Firecrawl; here we lock the decision
logic and the manual endpoint's contract (mirrors test_retry_escalation_route).
"""
import os
import sys
import uuid

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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun
from app.models.extraction_run import RunStatus
from app.services.firecrawl import _dedup_blocked, _should_auto_retry_blocked

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


# ── Pure logic (no DB) ───────────────────────────────────────────────────────

def test_dedup_blocked_preserves_order_and_drops_dupes_and_nulls():
    items = [{"url": "a"}, {"url": "a"}, {"url": "b"}, {"url": None}, {}, {"url": "c"}]
    assert [x["url"] for x in _dedup_blocked(items)] == ["a", "b", "c"]
    assert _dedup_blocked(None) == []
    assert _dedup_blocked([]) == []


def test_should_auto_retry_threshold():
    # 3 of 1000 = 0.3% ≤ 5% → retry
    assert _should_auto_retry_blocked(3, 1000, 5.0) is True
    # exactly at threshold (inclusive)
    assert _should_auto_retry_blocked(50, 1000, 5.0) is True
    # just over threshold → no retry
    assert _should_auto_retry_blocked(51, 1000, 5.0) is False
    # fully blocked → no retry
    assert _should_auto_retry_blocked(1000, 1000, 5.0) is False
    # nothing blocked, or unknown total, or disabled (0%) → no retry
    assert _should_auto_retry_blocked(0, 1000, 5.0) is False
    assert _should_auto_retry_blocked(3, 0, 5.0) is False
    assert _should_auto_retry_blocked(3, 1000, 0.0) is False


# ── Manual retry route (DB-backed) ───────────────────────────────────────────


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


async def _web_source_with_run(factory, blocked_pending):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="S",
                                  base_url="https://docs.example.com", source_type="web")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status=RunStatus.COMPLETED,
                            blocked_pending=blocked_pending)
        s.add(run); await s.commit()
        return src.id, run.id


@pytest.mark.asyncio
async def test_retry_blocked_enqueues_run_and_carries_list(client):
    c, factory = client
    pending = [{"url": "https://docs.example.com/a", "title": "A",
                "toc_entry_id": None, "sort_order": 1, "topic_key": None}]
    sid, rid = await _web_source_with_run(factory, pending)

    resp = await c.post(f"/api/extraction/runs/{rid}/retry-blocked")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    new_run_id = body["run_id"]
    assert new_run_id != str(rid)

    async with factory() as s:
        new_run = await s.get(ExtractionRun, uuid.UUID(new_run_id))
        assert new_run.kind == "retry_blocked"
        assert new_run.status == RunStatus.PENDING
        assert new_run.blocked_pending == pending   # carried onto the retry run


@pytest.mark.asyncio
async def test_retry_blocked_409_when_nothing_blocked(client):
    c, factory = client
    sid, rid = await _web_source_with_run(factory, None)
    resp = await c.post(f"/api/extraction/runs/{rid}/retry-blocked")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_blocked_404_unknown_run(client):
    c, _ = client
    resp = await c.post(f"/api/extraction/runs/{uuid.uuid4()}/retry-blocked")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_blocked_409_when_run_active(client):
    c, factory = client
    pending = [{"url": "https://docs.example.com/a", "title": "A",
                "toc_entry_id": None, "sort_order": 1, "topic_key": None}]
    sid, rid = await _web_source_with_run(factory, pending)
    async with factory() as s:
        s.add(ExtractionRun(source_id=sid, status=RunStatus.RUNNING))
        await s.commit()
    resp = await c.post(f"/api/extraction/runs/{rid}/retry-blocked")
    assert resp.status_code == 409
