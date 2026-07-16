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
from app.models.auth_realm import AuthRealm, RealmStatus
from app.models.extraction_run import RunStatus
from app.routes import extraction as extraction_route

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


async def _pdf_source_with_run(factory, escalation_pending, *, pdf_hash="deadbeef",
                               auth_realm_id=None):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="M",
                                  base_url="file://x.pdf", source_type="pdf",
                                  auth_realm_id=auth_realm_id)
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status=RunStatus.COMPLETED,
                            escalation_pending=escalation_pending, pdf_hash=pdf_hash)
        s.add(run); await s.commit()
        return src.id, run.id


async def test_retry_escalation_enqueues_escalate_run(client):
    c, factory = client
    pending = [{"article_id": str(uuid.uuid4()), "page_start": 4, "page_end": 5,
                "level": 1, "title": "Hardware Replacement"}]
    sid, rid = await _pdf_source_with_run(factory, pending)

    resp = await c.post(f"/api/extraction/runs/{rid}/retry-escalation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    new_run_id = body["run_id"]
    assert new_run_id != str(rid)

    async with factory() as s:
        new_run = await s.get(ExtractionRun, uuid.UUID(new_run_id))
        assert new_run.kind == "escalate"
        assert new_run.status == RunStatus.PENDING
        assert new_run.escalation_pending == pending   # carried onto the retry run
        assert new_run.pdf_hash == "deadbeef"          # hash carried so retry loads the cached PDF


async def test_retry_escalation_409_when_nothing_pending(client):
    c, factory = client
    sid, rid = await _pdf_source_with_run(factory, None)
    resp = await c.post(f"/api/extraction/runs/{rid}/retry-escalation")
    assert resp.status_code == 409


async def test_retry_escalation_404_unknown_run(client):
    c, _ = client
    resp = await c.post(f"/api/extraction/runs/{uuid.uuid4()}/retry-escalation")
    assert resp.status_code == 404


async def _expired_realm(factory) -> uuid.UUID:
    async with factory() as s:
        realm = AuthRealm(name="R", login_domain="x.com",
                          browserless_profile_name="p", status=RealmStatus.EXPIRED)
        s.add(realm); await s.commit()
        return realm.id


async def test_retry_escalation_bypasses_expired_auth_when_pdf_cached(client, monkeypatch):
    # Escalation re-extracts from the cached PDF, so an expired auth session must
    # NOT block it when that cached copy is present (the user's report: retry
    # should not require a fresh token / re-download).
    c, factory = client
    realm_id = await _expired_realm(factory)
    pending = [{"page_start": 4, "page_end": 5}]
    sid, rid = await _pdf_source_with_run(factory, pending, auth_realm_id=realm_id)
    monkeypatch.setattr(extraction_route.pdf_cache, "has_pdf", lambda h: True)

    resp = await c.post(f"/api/extraction/runs/{rid}/retry-escalation")
    assert resp.status_code == 200


async def test_retry_escalation_409_expired_auth_when_pdf_not_cached(client, monkeypatch):
    # No cached PDF → retry must re-download → a live login is genuinely required.
    c, factory = client
    realm_id = await _expired_realm(factory)
    pending = [{"page_start": 4, "page_end": 5}]
    sid, rid = await _pdf_source_with_run(factory, pending, auth_realm_id=realm_id)
    monkeypatch.setattr(extraction_route.pdf_cache, "has_pdf", lambda h: False)

    resp = await c.post(f"/api/extraction/runs/{rid}/retry-escalation")
    assert resp.status_code == 409
    assert "expired" in resp.json()["detail"].lower()


async def test_retry_escalation_409_when_run_active(client):
    # A pending/running run already holds the per-source active slot.
    c, factory = client
    pending = [{"article_id": str(uuid.uuid4()), "page_start": 0, "page_end": 0,
                "level": 1, "title": "X"}]
    sid, rid = await _pdf_source_with_run(factory, pending)
    async with factory() as s:
        s.add(ExtractionRun(source_id=sid, status=RunStatus.RUNNING))
        await s.commit()
    resp = await c.post(f"/api/extraction/runs/{rid}/retry-escalation")
    assert resp.status_code == 409
