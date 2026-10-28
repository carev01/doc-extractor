"""Bulk extract: queue ordinary extraction runs for many sources at once, from a
product or a vendor, skipping (and reporting) the ones a single trigger would
refuse. Runs go through the existing queue, so they still execute one at a time.
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.authz import Principal, get_principal
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import DocumentationSource, ExtractionRun, Product, Vendor
from app.models.auth_realm import AuthRealm, RealmStatus
from app.models.extraction_run import RunStatus
from app.models.user import UserRole
from app.models.user_vendor_permission import VendorAccessLevel

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

    async with factory() as s:
        v1 = Vendor(name="V1"); v2 = Vendor(name="V2"); s.add_all([v1, v2]); await s.flush()
        p1 = Product(vendor_id=v1.id, name="P1"); p2 = Product(vendor_id=v1.id, name="P2")
        p3 = Product(vendor_id=v2.id, name="P3"); s.add_all([p1, p2, p3]); await s.flush()
        realm = AuthRealm(name="R", login_domain="d", browserless_profile_name="r",
                          status=RealmStatus.EXPIRED)
        s.add(realm); await s.flush()
        a = DocumentationSource(product_id=p1.id, name="A", base_url="http://a")
        b = DocumentationSource(product_id=p1.id, name="B", base_url="http://b")
        c = DocumentationSource(product_id=p2.id, name="C", base_url="http://c", auth_realm_id=realm.id)
        d = DocumentationSource(product_id=p3.id, name="D", base_url="http://d")
        s.add_all([a, b, c, d]); await s.flush()
        s.add(ExtractionRun(source_id=a.id, status=RunStatus.PENDING))
        await s.commit()
        ids = dict(v1=v1.id, v2=v2.id, p1=p1.id, p2=p2.id, p3=p3.id, a=a.id, b=b.id, c=c.id, d=d.id)

    state = {"principal": ADMIN}

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_principal] = lambda: state["principal"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory, ids, state
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _active_runs(factory, source_id):
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(ExtractionRun).where(
            ExtractionRun.source_id == source_id,
            ExtractionRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED])))).scalar_one()


def _by_name(preview):
    return {s["source_name"]: s for p in preview["products"] for s in p["sources"]}


async def test_product_preview_marks_a_source_with_an_active_run(env):
    c, _, ids, _ = env
    r = await c.get(f"/api/extraction/bulk/preview?product_id={ids['p1']}")
    assert r.status_code == 200
    body = r.json()
    assert [p["product_name"] for p in body["products"]] == ["P1"]
    s = _by_name(body)
    assert (s["A"]["eligible"], s["A"]["reason"]) == (False, "active_run")
    assert (s["B"]["eligible"], s["B"]["reason"]) == (True, None)


async def test_vendor_preview_spans_its_products_and_flags_an_expired_login(env):
    c, _, ids, _ = env
    body = (await c.get(f"/api/extraction/bulk/preview?vendor_id={ids['v1']}")).json()
    assert sorted(p["product_name"] for p in body["products"]) == ["P1", "P2"]
    s = _by_name(body)
    assert set(s) == {"A", "B", "C"}  # not V2's D
    assert (s["C"]["eligible"], s["C"]["reason"]) == (False, "session_expired")


async def test_preview_needs_exactly_one_scope(env):
    c, _, ids, _ = env
    assert (await c.get("/api/extraction/bulk/preview")).status_code == 422
    both = await c.get(f"/api/extraction/bulk/preview?vendor_id={ids['v1']}&product_id={ids['p1']}")
    assert both.status_code == 422


async def test_bulk_queues_only_eligible_sources_and_never_a_second_run(env):
    c, factory, ids, _ = env
    r = await c.post("/api/extraction/bulk", json={"source_ids": [str(ids[k]) for k in ("a", "b", "c")]})
    assert r.status_code == 200
    body = r.json()
    assert [q["source_id"] for q in body["queued"]] == [str(ids["b"])]
    assert {s["source_id"]: s["reason"] for s in body["skipped"]} == {
        str(ids["a"]): "active_run", str(ids["c"]): "session_expired"}
    assert await _active_runs(factory, ids["a"]) == 1
    assert await _active_runs(factory, ids["b"]) == 1
    assert await _active_runs(factory, ids["c"]) == 0
    async with factory() as s:
        run = (await s.execute(select(ExtractionRun).where(ExtractionRun.source_id == ids["b"]))).scalar_one()
    assert (run.kind, run.trigger, run.allow_toc_collapse) == ("extract", "bulk", False)


async def test_read_only_grant_skips_instead_of_queueing(env):
    c, factory, ids, state = env
    state["principal"] = Principal(see_all=False, role=UserRole.READ_WRITE,
                                   vendor_levels={ids["v1"]: VendorAccessLevel.READ_ONLY})
    s = _by_name((await c.get(f"/api/extraction/bulk/preview?product_id={ids['p1']}")).json())
    assert s["B"]["reason"] == "no_write_access"
    body = (await c.post("/api/extraction/bulk", json={"source_ids": [str(ids["b"])]})).json()
    assert body["queued"] == []
    assert body["skipped"] == [{"source_id": str(ids["b"]), "reason": "no_write_access"}]
    assert await _active_runs(factory, ids["b"]) == 0


async def test_invisible_sources_are_not_found_not_forbidden(env):
    # A vendor the caller cannot read is 404-invisible everywhere else; bulk
    # must not reveal that its sources exist.
    c, factory, ids, state = env
    state["principal"] = Principal(see_all=False, role=UserRole.READ_WRITE,
                                   vendor_levels={ids["v1"]: VendorAccessLevel.READ_WRITE})
    assert (await c.get(f"/api/extraction/bulk/preview?product_id={ids['p3']}")).status_code == 404
    assert (await c.get(f"/api/extraction/bulk/preview?vendor_id={ids['v2']}")).status_code == 404
    body = (await c.post("/api/extraction/bulk",
                         json={"source_ids": [str(ids["d"]), str(uuid.uuid4())]})).json()
    assert body["queued"] == []
    assert {s["reason"] for s in body["skipped"]} == {"not_found"}
    assert await _active_runs(factory, ids["d"]) == 0
