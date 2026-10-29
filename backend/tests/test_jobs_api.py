"""Jobs API (tiers/coverage/schedule era): shapes, validation, coverage, catalog."""
import os
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
from app.models import (
    DocumentationSource, ExtractionRun, JobRun, JobTarget, Product, Vendor,
)
from app.models.auth_realm import AuthRealm, RealmStatus
from app.models.extraction_run import RunStatus
from app.models.job_run import JobRunStatus
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
    async with factory() as s:
        v1 = Vendor(name="V1"); v2 = Vendor(name="V2"); s.add_all([v1, v2]); await s.flush()
        p1 = Product(vendor_id=v1.id, name="P1"); p2 = Product(vendor_id=v2.id, name="P2")
        s.add_all([p1, p2]); await s.flush()
        realm = AuthRealm(name="Portal", login_domain="d", browserless_profile_name="r",
                          status=RealmStatus.ACTIVE)
        s.add(realm); await s.flush()
        a = DocumentationSource(product_id=p1.id, name="A", base_url="http://a")
        b = DocumentationSource(product_id=p1.id, name="B", base_url="http://b", auth_realm_id=realm.id)
        c = DocumentationSource(product_id=p2.id, name="C", base_url="http://c")
        s.add_all([a, b, c]); await s.commit()
        ids = dict(v1=v1.id, v2=v2.id, p1=p1.id, p2=p2.id, a=a.id, b=b.id, c=c.id)
    state = {"principal": ADMIN}

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_principal] = lambda: state["principal"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as cl:
        yield cl, factory, {k: str(v) for k, v in ids.items()}, state
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _job(c, **kw):
    r = await c.post("/api/jobs", json={"name": kw.pop("name", "J"), **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_response_shape(env):
    c, _, _, _ = env
    j = await _job(c, enabled=True, schedule_kind="daily", time_of_day="03:30")
    assert j["cron"] == "30 3 * * *" and j["next_run_at"]
    assert j["targets"] == {"vendor_ids": [], "product_ids": [], "source_ids": []}
    assert j["coverage"] == {"sources": 0, "products": 0, "overlap_sources": 0, "login_sources": 0}
    assert len(j["next_runs"]) == 3
    assert (await c.get(f"/api/jobs/{j['id']}")).json()["next_runs"] == j["next_runs"]


async def test_jobs_admin_only(env):
    c, _, _, state = env
    state["principal"] = Principal(see_all=True, role=UserRole.READ_WRITE)
    assert (await c.get("/api/jobs")).status_code == 403
    assert (await c.get("/api/jobs/catalog")).status_code == 403
    assert (await c.post("/api/jobs/schedule/preview", json={"kind": "daily"})).status_code == 403


async def test_interval_5_is_422_with_message(env):
    c, _, _, _ = env
    r = await c.post("/api/jobs", json={
        "name": "J", "enabled": True, "schedule_kind": "monthly_day",
        "interval_months": 5, "day_of_month": 3})
    assert r.status_code == 422 and "interval" in r.json()["detail"].lower()


async def test_start_month_defaults_to_next_month(env):
    c, _, _, _ = env
    j = await _job(c, enabled=True, schedule_kind="monthly_day", interval_months=3,
                   day_of_month=5, timezone="America/Sao_Paulo")
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    assert j["start_month"] == now.month % 12 + 1
    # switching to a monthly kind without start_month also defaults it
    d = await _job(c, name="D", enabled=True, schedule_kind="daily")
    assert d["start_month"] is None
    r = await c.patch(f"/api/jobs/{d['id']}", json={
        "schedule_kind": "monthly_day", "interval_months": 2, "day_of_month": 1})
    assert r.status_code == 200 and r.json()["start_month"] == datetime.now(ZoneInfo("UTC")).month % 12 + 1


async def test_unknown_tier_is_404(env):
    c, _, _, _ = env
    bad = str(uuid.uuid4())
    assert (await c.post("/api/jobs", json={"name": "J", "tier_id": bad})).status_code == 404
    j = await _job(c)
    assert (await c.patch(f"/api/jobs/{j['id']}", json={"tier_id": bad})).status_code == 404


@pytest.mark.parametrize("fields", [
    {"time_of_day": "25:00"}, {"time_of_day": "nope"}, {"timezone": "Mars/Base"},
    {"weekday": 9}, {"day_of_month": 31}, {"interval_months": 5}, {"start_month": 13},
])
async def test_garbage_rejected_even_without_schedule_kind(env, fields):
    c, _, _, _ = env
    r = await c.post("/api/jobs", json={"name": "J", **fields})
    assert r.status_code == 422, r.text
    j = await _job(c)
    assert (await c.patch(f"/api/jobs/{j['id']}", json=fields)).status_code == 422


async def test_kind_change_clears_unused_fields(env):
    c, _, _, _ = env
    j = await _job(c, enabled=True, schedule_kind="weekly", weekday=2)
    assert j["weekday"] == 2
    r = await c.patch(f"/api/jobs/{j['id']}", json={
        "schedule_kind": "monthly_day", "interval_months": 1, "day_of_month": 4})
    b = r.json()
    assert r.status_code == 200 and b["weekday"] is None and b["nth"] is None
    assert b["cron"] == "0 2 4 * *"
    r = await c.patch(f"/api/jobs/{j['id']}", json={"schedule_kind": "daily"})
    b = r.json()
    assert b["day_of_month"] is None and b["interval_months"] is None and b["start_month"] is None


async def test_schedule_preview(env):
    c, _, _, _ = env
    r = await c.post("/api/jobs/schedule/preview", json={
        "kind": "monthly_nth", "weekday": 1, "nth": "first", "interval_months": 3,
        "start_month": 1, "time_of_day": "04:00"})
    assert r.status_code == 200 and len(r.json()["next_runs"]) == 3
    r = await c.post("/api/jobs/schedule/preview", json={
        "kind": "monthly_day", "interval_months": 5, "day_of_month": 3})
    assert r.status_code == 422 and "interval" in r.json()["detail"].lower()
    r = await c.post("/api/jobs/schedule/preview", json={"kind": "weekly"})
    assert r.status_code == 422


async def test_coverage_put_normalizes_and_replaces(env):
    c, _, ids, _ = env
    j = await _job(c)
    r = await c.put(f"/api/jobs/{j['id']}/coverage", json={"source_ids": [ids["a"], ids["b"]]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["targets"] == {"vendor_ids": [ids["v1"]], "product_ids": [], "source_ids": []}  # whole vendor collapses
    assert b["coverage"] == {"sources": 2, "products": 1, "overlap_sources": 0, "login_sources": 1}
    r = await c.put(f"/api/jobs/{j['id']}/coverage", json={"source_ids": [ids["c"]]})
    t = r.json()["targets"]
    assert t["product_ids"] == [] and (t["vendor_ids"] == [ids["v2"]] or t["source_ids"] == [ids["c"]])
    r = await c.put(f"/api/jobs/{j['id']}/coverage", json={"source_ids": []})
    assert r.json()["coverage"]["sources"] == 0


async def test_coverage_unknown_source_rejected_and_targets_kept(env):
    c, _, ids, _ = env
    j = await _job(c)
    await c.put(f"/api/jobs/{j['id']}/coverage", json={"source_ids": [ids["a"]]})
    bad = str(uuid.uuid4())
    r = await c.put(f"/api/jobs/{j['id']}/coverage", json={"source_ids": [ids["c"], bad]})
    assert r.status_code == 404 and bad in r.json()["detail"]
    assert (await c.get(f"/api/jobs/{j['id']}")).json()["coverage"]["sources"] == 1


async def test_overlap_sources_when_two_jobs_share(env):
    c, _, ids, _ = env
    j1, j2 = await _job(c, name="J1"), await _job(c, name="J2")
    await c.put(f"/api/jobs/{j1['id']}/coverage", json={"source_ids": [ids["a"], ids["b"]]})
    await c.put(f"/api/jobs/{j2['id']}/coverage", json={"source_ids": [ids["b"], ids["c"]]})
    jobs = {x["name"]: x for x in (await c.get("/api/jobs")).json()["jobs"]}
    assert jobs["J1"]["coverage"]["overlap_sources"] == 1
    assert jobs["J2"]["coverage"]["overlap_sources"] == 1
    one = (await c.get(f"/api/jobs/{j1['id']}")).json()
    assert one["coverage"]["overlap_sources"] == 1


async def test_catalog(env):
    c, factory, ids, _ = env
    r = await c.get("/api/jobs/catalog")
    assert r.status_code == 200
    v1 = next(v for v in r.json() if v["vendor_name"] == "V1")
    assert v1["tier_id"] is None
    srcs = {s["source_name"]: s for s in v1["products"][0]["sources"]}
    assert srcs["A"]["auth_realm"] is None and srcs["B"]["auth_realm"] == "Portal"
    assert v1["products"][0]["product_name"] == "P1"


async def test_job_run_children_paused_and_skipped(env):
    c, factory, ids, _ = env
    j = await _job(c)
    async with factory() as s:
        jr = JobRun(job_id=uuid.UUID(j["id"]), status=JobRunStatus.RUNNING, trigger="manual",
                    sources_total=2, skipped=[{"source_id": ids["c"], "reason": "paused_run"}])
        s.add(jr); await s.flush()
        s.add(ExtractionRun(source_id=uuid.UUID(ids["a"]), status=RunStatus.PAUSED, job_run_id=jr.id))
        s.add(ExtractionRun(source_id=uuid.UUID(ids["b"]), status=RunStatus.RUNNING, job_run_id=jr.id))
        await s.commit()
    for url in (f"/api/jobs/{j['id']}/runs", "/api/jobs/runs"):
        row = (await c.get(url)).json()[0]
        assert row["children_paused"] == 1
        assert row["skipped"] == [{"source_id": ids["c"], "reason": "paused_run"}]
    j2 = await _job(c, name="Empty")
    async with factory() as s:
        s.add(JobRun(job_id=uuid.UUID(j2["id"]), status=JobRunStatus.COMPLETED)); await s.commit()
    row = (await c.get(f"/api/jobs/{j2['id']}/runs")).json()[0]
    assert row["children_paused"] == 0 and row["skipped"] == []


@pytest.mark.parametrize("field", ["name", "enabled"])
async def test_patch_null_not_null_field_is_422(env, field):
    """name / enabled are NOT NULL: an explicit null must be refused, not 500."""
    c, _, _, _ = env
    j = await _job(c, name="Keep", enabled=True, schedule_kind="daily")
    r = await c.patch(f"/api/jobs/{j['id']}", json={field: None})
    assert r.status_code == 422, r.text
    got = (await c.get(f"/api/jobs/{j['id']}")).json()
    assert got["name"] == "Keep" and got["enabled"] is True
