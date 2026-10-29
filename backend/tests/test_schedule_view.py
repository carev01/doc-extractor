"""Schedule view: per-product last-ok / latest / next aggregation."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.authz import Principal, get_principal
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import (
    DocumentationSource, ExtractionRun, Job, JobRun, JobTarget, Product, Vendor,
)
from app.models.extraction_run import RunStatus
from app.models.user import UserRole

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_principal] = lambda: Principal(see_all=True, role=UserRole.ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as cl:
        yield cl, factory, statements
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _product(s, n_sources=2, name="P"):
    v = Vendor(name=f"V-{name}"); s.add(v); await s.flush()
    p = Product(vendor_id=v.id, name=name); s.add(p); await s.flush()
    srcs = [DocumentationSource(product_id=p.id, name=f"S{i}", base_url=f"http://{name}/{i}")
            for i in range(n_sources)]
    s.add_all(srcs); await s.flush()
    return v, p, srcs


def _run(src, ago_h, kind="extract", status=RunStatus.COMPLETED, blocked=None, **kw):
    t = NOW - timedelta(hours=ago_h)
    return ExtractionRun(source_id=src.id, kind=kind, status=status, started_at=t,
                         completed_at=t, blocked_pending=blocked, **kw)


async def _job(s, name, targets, enabled=True, nxt=None):
    j = Job(name=name, enabled=enabled, next_run_at=nxt); s.add(j); await s.flush()
    s.add_all([JobTarget(job_id=j.id, **t) for t in targets]); await s.flush()
    return j


async def _get(c):
    r = await c.get("/api/jobs/schedule")
    assert r.status_code == 200, r.text
    return {p["product_name"]: p for p in r.json()["products"]}


def _ts(d):
    return datetime.fromisoformat(d)


async def test_least_fresh_source_drives_product_last_ok(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a, b) = await _product(s)
        s.add_all([_run(a, 1), _run(b, 30)]); await s.commit()
    p = (await _get(c))["P"]
    assert _ts(p["last_ok_at"]) == NOW - timedelta(hours=30)
    assert _ts(p["latest"]["at"]) == NOW - timedelta(hours=1)


async def test_never_ok_source_nulls_product_last_ok(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a, b) = await _product(s)
        s.add(_run(a, 1)); await s.commit()
    assert (await _get(c))["P"]["last_ok_at"] is None


async def test_enrich_runs_do_not_count(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a,) = await _product(s, 1)
        s.add_all([_run(a, 10), _run(a, 1, kind="enrich"), _run(a, 2, kind="escalate")])
        await s.commit()
    p = (await _get(c))["P"]
    assert _ts(p["last_ok_at"]) == NOW - timedelta(hours=10)
    assert _ts(p["latest"]["at"]) == NOW - timedelta(hours=10)


async def test_blocked_pages_make_it_warn(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a,) = await _product(s, 1)
        s.add_all([_run(a, 20), _run(a, 1, blocked=[{"url": "http://x"}])]); await s.commit()
    p = (await _get(c))["P"]
    assert p["latest"]["outcome"] == "warn"
    assert _ts(p["last_ok_at"]) == NOW - timedelta(hours=20)


async def test_failed_run_is_latest_outcome(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a,) = await _product(s, 1)
        s.add_all([_run(a, 20), _run(a, 1, status=RunStatus.FAILED)]); await s.commit()
    assert (await _get(c))["P"]["latest"]["outcome"] == "failed"


async def test_skip_reason_shows_as_latest(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a,) = await _product(s, 1)
        j = await _job(s, "Nightly", [{"source_id": a.id}])
        s.add(_run(a, 20))
        s.add(JobRun(job_id=j.id, created_at=NOW - timedelta(hours=1),
                     skipped=[{"source_id": str(a.id), "reason": "login_needed"}]))
        await s.commit()
    latest = (await _get(c))["P"]["latest"]
    assert latest["outcome"] == "skipped" and latest["reason"] == "login_needed"
    assert latest["job_name"] == "Nightly"


async def test_next_is_latest_among_sources_and_partly_scheduled_flag(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a, b, d) = await _product(s, 3)
        await _job(s, "J1", [{"source_id": a.id}], nxt=NOW + timedelta(hours=2))
        await _job(s, "J1b", [{"source_id": a.id}], nxt=NOW + timedelta(hours=9))
        await _job(s, "J2", [{"source_id": b.id}], nxt=NOW + timedelta(hours=5))
        await s.commit()
    p = (await _get(c))["P"]
    assert _ts(p["next_at"]) == NOW + timedelta(hours=5)
    assert p["scheduled_sources"] == 2 and p["source_count"] == 3
    assert {x["source_name"]: x["next_at"] is not None for x in p["sources"]} == \
        {"S0": True, "S1": True, "S2": False}
    assert _ts(p["sources"][0]["next_at"]) == NOW + timedelta(hours=2)
    assert p["jobs"] == ["J1", "J1b", "J2"]


async def test_disabled_job_does_not_schedule(env):
    c, f, _ = env
    async with f() as s:
        _, _, (a,) = await _product(s, 1)
        await _job(s, "Off", [{"source_id": a.id}], enabled=False, nxt=NOW + timedelta(hours=1))
        await s.commit()
    p = (await _get(c))["P"]
    assert p["next_at"] is None and p["scheduled_sources"] == 0


async def test_product_with_no_sources(env):
    c, f, _ = env
    async with f() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        s.add(Product(vendor_id=v.id, name="Empty")); await s.commit()
    p = (await _get(c))["Empty"]
    assert p["source_count"] == 0 and p["last_ok_at"] is None and p["latest"] is None
    assert p["next_at"] is None and p["sources"] == [] and p["tier_name"] is None


async def test_query_count_is_constant(env):
    c, f, statements = env

    async def seed(n, tag):
        async with f() as s:
            for i in range(n):
                _, _, (a, b) = await _product(s, 2, name=f"{tag}{i}")
                s.add_all([_run(a, 1), _run(b, 2)])
                await _job(s, f"{tag}j{i}", [{"source_id": a.id}], nxt=NOW + timedelta(hours=1))
            await s.commit()

    async def count():
        statements.clear()
        assert (await c.get("/api/jobs/schedule")).status_code == 200
        return len(statements)

    await seed(3, "a")
    small = await count()
    await seed(27, "b")
    big = await count()
    assert small == big
