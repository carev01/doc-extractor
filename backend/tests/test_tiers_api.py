"""Tiers API: CRUD, in-use deletion guard, bulk vendor assignment, admin-only."""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.authz import Principal, get_principal
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Job, Vendor
from app.models.user import UserRole

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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory, state
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _vendor(factory, name):
    async with factory() as s:
        v = Vendor(name=name); s.add(v); await s.commit()
        return v.id


async def _tier(c, name="Tier 1"):
    r = await c.post("/api/tiers", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_tier_crud(env):
    c, _, _ = env
    t1 = await _tier(c, "Tier 1")
    t2 = await _tier(c, "Tier 2")
    r = await c.get("/api/tiers")
    assert [t["name"] for t in r.json()] == ["Tier 1", "Tier 2"]
    r = await c.patch(f"/api/tiers/{t2}", json={"name": "Gold"})
    assert r.status_code == 200 and r.json()["name"] == "Gold"
    assert (await c.post("/api/tiers", json={"name": "Tier 1"})).status_code == 409
    assert (await c.patch(f"/api/tiers/{t2}", json={"name": "Tier 1"})).status_code == 409
    assert (await c.delete(f"/api/tiers/{t2}")).status_code == 204
    assert (await c.delete(f"/api/tiers/{t2}")).status_code == 404


async def test_deleting_a_tier_in_use_is_refused(env):
    c, factory, _ = env
    tid = await _tier(c)
    vid = await _vendor(factory, "V")
    assert (await c.put(f"/api/tiers/{tid}/vendors", json={"vendor_ids": [str(vid)]})).status_code == 200
    r = await c.post("/api/jobs", json={"name": "J", "tier_id": tid})
    assert r.status_code == 201
    r = await c.delete(f"/api/tiers/{tid}")
    assert r.status_code == 409
    body = r.json()
    assert body["vendors"] == 1 and body["jobs"] == 1 and body["detail"]
    listed = (await c.get("/api/tiers")).json()[0]
    assert listed["vendor_count"] == 1 and listed["job_count"] == 1


async def test_bulk_set_and_clear_tier(env):
    c, factory, _ = env
    tid = await _tier(c)
    v1, v2 = await _vendor(factory, "V1"), await _vendor(factory, "V2")
    r = await c.put(f"/api/tiers/{tid}/vendors", json={"vendor_ids": [str(v1), str(v2)]})
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert (await c.get(f"/api/vendors/{v1}")).json()["tier_id"] == tid
    r = await c.put("/api/tiers/none/vendors", json={"vendor_ids": [str(v1)]})
    assert r.status_code == 200
    assert (await c.get(f"/api/vendors/{v1}")).json()["tier_id"] is None
    assert (await c.get(f"/api/vendors/{v2}")).json()["tier_id"] == tid


async def test_unknown_vendor_ids_rejected_and_nothing_applied(env):
    c, factory, _ = env
    tid = await _tier(c)
    v1 = await _vendor(factory, "V1")
    bad = str(uuid.uuid4())
    r = await c.put(f"/api/tiers/{tid}/vendors", json={"vendor_ids": [str(v1), bad]})
    assert r.status_code == 404 and bad in r.json()["detail"]
    assert (await c.get(f"/api/vendors/{v1}")).json()["tier_id"] is None
    assert (await c.put(f"/api/tiers/{uuid.uuid4()}/vendors", json={"vendor_ids": []})).status_code == 404


async def test_tiers_admin_only(env):
    c, _, state = env
    state["principal"] = Principal(see_all=True, role=UserRole.READ_WRITE)
    assert (await c.get("/api/tiers")).status_code == 403
    assert (await c.post("/api/tiers", json={"name": "x"})).status_code == 403
