"""fan_out_job: coverage-based fan-out with recorded skip reasons."""

import os
import sys
import time
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    AuthRealm, DocumentationSource, ExtractionRun, Job, JobRun, JobTarget, Product, Vendor,
)
from app.models.auth_realm import RealmStatus
from app.models.extraction_run import RunStatus
from app.models.job_run import JobRunStatus
from app.services.scheduling import fan_out_job, job_run_children_summary, reconcile_job_runs

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 6, 17, 5, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.core import crypto
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_cipher", None)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _setup(db, realm=None):
    sfx = uuid.uuid4().hex[:8]
    v = Vendor(name=f"V-{sfx}")
    db.add(v)
    await db.flush()
    p = Product(vendor_id=v.id, name=f"P-{sfx}")
    db.add(p)
    await db.flush()
    s = DocumentationSource(
        product_id=p.id, name="S", base_url=f"https://x/{sfx}",
        auth_realm_id=realm.id if realm else None,
    )
    db.add(s)
    job = Job(name="J")
    db.add(job)
    await db.flush()
    db.add(JobTarget(job_id=job.id, source_id=s.id))
    await db.flush()
    return job, s


async def _realm(db, **kw):
    r = AuthRealm(
        name=f"R-{uuid.uuid4().hex[:6]}", login_domain="x.com",
        browserless_profile_name=f"p-{uuid.uuid4().hex[:6]}", **kw,
    )
    db.add(r)
    await db.flush()
    return r


def _expired_snapshot():
    return {"cookies": [{"name": "session_token", "expires": time.time() - 3600}]}


async def test_job_with_no_coverage_returns_none(factory):
    async with factory() as db:
        job = Job(name="Empty")
        db.add(job)
        await db.flush()
        assert await fan_out_job(db, job, "scheduled", now=NOW) is None


async def test_paused_run_is_skipped_not_doubled(factory):
    async with factory() as db:
        job, s = await _setup(db)
        db.add(ExtractionRun(source_id=s.id, status=RunStatus.PAUSED, trigger="manual"))
        await db.flush()
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert jr.skipped == [{"source_id": str(s.id), "reason": "paused_run"}]
        assert jr.sources_total == 0
        n = (await db.execute(select(ExtractionRun).where(ExtractionRun.source_id == s.id))).scalars().all()
        assert len(n) == 1


async def test_active_run_coalesces(factory):
    async with factory() as db:
        job, s = await _setup(db)
        db.add(ExtractionRun(source_id=s.id, status=RunStatus.RUNNING, trigger="manual"))
        await db.flush()
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert jr.skipped == [{"source_id": str(s.id), "reason": "active_run"}]
        assert jr.sources_total == 0
        assert jr.status == JobRunStatus.COMPLETED


async def test_login_failed_realm_skipped(factory):
    async with factory() as db:
        r = await _realm(db, status=RealmStatus.LOGIN_FAILED, username="u", password="p")
        job, s = await _setup(db, r)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert jr.skipped == [{"source_id": str(s.id), "reason": "login_failed"}]
        assert jr.sources_total == 0


async def test_cookie_realm_expired_skipped_as_login_needed(factory):
    async with factory() as db:
        r = await _realm(db, status=RealmStatus.EXPIRED, state_snapshot=_expired_snapshot())
        job, s = await _setup(db, r)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert jr.skipped == [{"source_id": str(s.id), "reason": "login_needed"}]
        assert jr.sources_total == 0


async def test_credential_realm_expired_is_enqueued(factory):
    async with factory() as db:
        r = await _realm(
            db, status=RealmStatus.EXPIRED, username="u", password="p",
            state_snapshot=_expired_snapshot(),
        )
        job, s = await _setup(db, r)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert not jr.skipped
        assert jr.sources_total == 1
        run = (await db.execute(select(ExtractionRun).where(ExtractionRun.source_id == s.id))).scalar_one()
        assert run.job_run_id == jr.id


async def test_all_children_paused_jobrun_reports_children_paused(factory):
    async with factory() as db:
        job, s = await _setup(db)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        run = (await db.execute(select(ExtractionRun).where(ExtractionRun.job_run_id == jr.id))).scalar_one()
        run.status = RunStatus.PAUSED
        await db.flush()
        other = JobRun(job_id=job.id, trigger="scheduled", status=JobRunStatus.PENDING)
        db.add(other)
        await db.flush()
        summary = await job_run_children_summary(db, [jr.id, other.id])
        assert summary[jr.id]["paused"] == 1
        assert summary[jr.id]["total"] == 1
        assert summary[other.id]["paused"] == 0
        await reconcile_job_runs(db, NOW)
        await db.refresh(jr)
        assert jr.status == JobRunStatus.RUNNING


@pytest.mark.parametrize("status,snapshot", [
    (RealmStatus.NEEDS_LOGIN, None),                  # never logged in
    (RealmStatus.NEEDS_LOGIN, {"cookies": []}),        # status still says so
    (RealmStatus.ACTIVE, None),                        # no stored session at all
])
async def test_uncredentialed_realm_without_session_is_login_needed(factory, status, snapshot):
    """No saved credentials and no usable session: ensure_session would raise
    NeedsLoginError, so the run must not be enqueued only to FAIL."""
    async with factory() as db:
        r = await _realm(db, status=status, state_snapshot=snapshot)
        job, s = await _setup(db, r)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert jr.skipped == [{"source_id": str(s.id), "reason": "login_needed"}]
        assert jr.sources_total == 0


async def test_credentialed_needs_login_realm_is_enqueued(factory):
    async with factory() as db:
        r = await _realm(db, status=RealmStatus.NEEDS_LOGIN, username="u", password="p")
        job, s = await _setup(db, r)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert not jr.skipped and jr.sources_total == 1


async def test_active_uncredentialed_realm_with_session_is_enqueued(factory):
    async with factory() as db:
        r = await _realm(db, status=RealmStatus.ACTIVE, state_snapshot={
            "cookies": [{"name": "session_token", "expires": time.time() + 86400}]})
        job, s = await _setup(db, r)
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert not jr.skipped and jr.sources_total == 1


async def _second_source(db, job, realm=None):
    s = await db.get(DocumentationSource, (await db.execute(
        select(JobTarget.source_id).where(JobTarget.job_id == job.id))).scalar_one())
    s2 = DocumentationSource(product_id=s.product_id, name="S2", base_url=s.base_url + "/2",
                             auth_realm_id=realm.id if realm else None)
    db.add(s2)
    await db.flush()
    db.add(JobTarget(job_id=job.id, source_id=s2.id))
    await db.flush()
    return s2


async def test_mixed_firing_coalesces_one_and_enqueues_the_other(factory):
    async with factory() as db:
        job, s1 = await _setup(db)
        s2 = await _second_source(db, job)
        db.add(ExtractionRun(source_id=s1.id, status=RunStatus.RUNNING, trigger="manual"))
        await db.flush()
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        assert jr.skipped == [{"source_id": str(s1.id), "reason": "active_run"}]
        assert jr.sources_total == 1
        assert jr.status == JobRunStatus.PENDING and jr.completed_at is None
        child = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.job_run_id == jr.id))).scalar_one()
        assert child.source_id == s2.id and child.status == RunStatus.PENDING
        # The savepoint rolled back only s1's insert: its running run is untouched.
        s1_runs = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.source_id == s1.id))).scalars().all()
        assert [r.status for r in s1_runs] == [RunStatus.RUNNING]


async def test_skip_precedence_is_pinned(factory):
    """Current precedence: paused_run > login_failed > login_needed > active_run.
    The realm checks run before the enqueue is tried, so a source with an active
    run AND a LOGIN_FAILED realm is reported as login_failed."""
    async with factory() as db:
        r = await _realm(db, status=RealmStatus.LOGIN_FAILED, username="u", password="p")
        job, active = await _setup(db, r)
        paused = await _second_source(db, job, r)
        db.add_all([
            ExtractionRun(source_id=active.id, status=RunStatus.RUNNING, trigger="manual"),
            ExtractionRun(source_id=paused.id, status=RunStatus.PAUSED, trigger="manual"),
        ])
        await db.flush()
        jr = await fan_out_job(db, job, "scheduled", now=NOW)
        reasons = {d["source_id"]: d["reason"] for d in jr.skipped}
        assert reasons == {str(active.id): "login_failed", str(paused.id): "paused_run"}

        r2 = await _realm(db, status=RealmStatus.EXPIRED, state_snapshot=_expired_snapshot())
        job2, s3 = await _setup(db, r2)
        db.add(ExtractionRun(source_id=s3.id, status=RunStatus.RUNNING, trigger="manual"))
        await db.flush()
        jr2 = await fan_out_job(db, job2, "scheduled", now=NOW)
        assert jr2.skipped == [{"source_id": str(s3.id), "reason": "login_needed"}]


async def test_children_summary_many_jobruns_one_query(factory):
    from sqlalchemy import event
    async with factory() as db:
        job, s = await _setup(db)
        jrs = []
        layout = [
            [RunStatus.COMPLETED, RunStatus.COMPLETED, RunStatus.FAILED],
            [RunStatus.PAUSED, RunStatus.RUNNING],
            [RunStatus.CANCELLED],
            [],
        ]
        for statuses in layout:
            jr = JobRun(job_id=job.id, trigger="scheduled", status=JobRunStatus.RUNNING)
            db.add(jr)
            await db.flush()
            for st in statuses:
                db.add(ExtractionRun(source_id=s.id, status=st, trigger="scheduled",
                                     job_run_id=jr.id))
            jrs.append(jr)
        await db.flush()
        statements: list[str] = []

        def capture(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            summary = await job_run_children_summary(db, [jr.id for jr in jrs])
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        a, b, c, d = (summary[jr.id] for jr in jrs)
        assert (a["total"], a["completed"], a["failed"], a["paused"]) == (3, 2, 1, 0)
        assert (b["total"], b["paused"], b["running"], b["completed"]) == (2, 1, 1, 0)
        assert (c["total"], c["cancelled"]) == (1, 1)
        assert d["total"] == 0 and all(v == 0 for v in d.values())
