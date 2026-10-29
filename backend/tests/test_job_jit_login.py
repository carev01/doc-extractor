"""Just-in-time scripted login for job-triggered runs.

`realm_manager.session_for_run` logs a realm in shortly before a job's run when
its session is missing or expires within the horizon — at most once per realm per
JobRun, never for a LOGIN_FAILED realm, never for a manual run. The mid-run auth
wall re-logs a job run in once and re-queues it before falling back to PAUSE.
No Browserless call is ever made: `run_scripted_login` is replaced by a fake.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    AuthRealm, DocumentationSource, ExtractionRun, Job, JobRun, Product, Vendor,
)
from app.models.auth_realm import RealmStatus
from app.models.extraction_run import RunStatus
from app.services import firecrawl as firecrawl_mod
from app.services.auth import realm_manager
from app.services.auth.realm_manager import NeedsLoginError

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.core import crypto
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_cipher", None)


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    async def _quiet(*a, **kw):
        return None
    monkeypatch.setattr(firecrawl_mod, "notify", _quiet)


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


def _snapshot(expires_in: timedelta) -> dict:
    exp = (datetime.now(timezone.utc) + expires_in).timestamp()
    return {"cookies": [{"name": "session_token", "value": "v", "expires": exp}]}


class FakeLogin:
    """Counting stand-in for run_scripted_login (mirrors its persistence)."""

    def __init__(self, ok: bool = True, fresh_expiry: timedelta = timedelta(minutes=10)):
        self.calls = 0
        self.ok = ok
        self.fresh_expiry = fresh_expiry

    async def __call__(self, db, realm):
        self.calls += 1
        if not self.ok:
            await realm_manager.invalidate(db, realm, RealmStatus.LOGIN_FAILED, "nope")
            raise NeedsLoginError("fake login failed")
        # A short-lived fresh session: a second run of the same JobRun would still
        # see it "expiring within the hour", so only the once-per-JobRun rule
        # stops a second login.
        realm.state_snapshot = _snapshot(self.fresh_expiry)
        realm.status = RealmStatus.ACTIVE
        realm.last_login_at = datetime.now(timezone.utc)
        await db.flush()


@pytest.fixture
def fake_login(monkeypatch):
    fake = FakeLogin()
    monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
    return fake


async def _setup(db, *, status=RealmStatus.ACTIVE, expires_in=timedelta(minutes=20),
                 creds=True, last_login_at=None):
    sfx = uuid.uuid4().hex[:8]
    realm = AuthRealm(
        name=f"R-{sfx}", login_domain="x.com", browserless_profile_name=f"p-{sfx}",
        status=status, state_snapshot=_snapshot(expires_in),
        last_login_at=last_login_at,
    )
    if creds:
        realm.username = "user"
        realm.password = "secret"
    db.add(realm)
    v = Vendor(name=f"V-{sfx}")
    db.add(v)
    await db.flush()
    p = Product(vendor_id=v.id, name=f"P-{sfx}")
    db.add(p)
    await db.flush()
    src = DocumentationSource(
        product_id=p.id, name="S", base_url=f"https://x/{sfx}", auth_realm_id=realm.id,
    )
    db.add(src)
    job = Job(name="J")
    db.add(job)
    await db.flush()
    jr = JobRun(job_id=job.id, trigger="scheduled",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    db.add(jr)
    await db.flush()
    return realm, src, jr


async def _run(db, src, job_run=None, status=RunStatus.RUNNING):
    r = ExtractionRun(source_id=src.id, status=status, trigger="scheduled",
                      job_run_id=job_run.id if job_run else None)
    db.add(r)
    await db.flush()
    return r


async def test_manual_run_never_logs_in(factory, fake_login):
    async with factory() as db:
        realm, src, _ = await _setup(db, expires_in=timedelta(minutes=5))
        run = await _run(db, src, job_run=None)
        state = await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 0
        assert state == realm.state_snapshot


async def test_job_run_logs_in_when_expiring_within_an_hour(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(minutes=30))
        run = await _run(db, src, job_run=jr)
        state = await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 1
        assert realm.status == RealmStatus.ACTIVE
        assert state == realm.state_snapshot


async def test_job_run_keeps_long_lived_session(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 0


async def test_job_run_without_credentials_does_not_log_in(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(minutes=5), creds=False)
        run = await _run(db, src, job_run=jr)
        await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 0


async def test_login_failed_realm_is_not_retried(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, status=RealmStatus.LOGIN_FAILED,
                                      expires_in=timedelta(minutes=5))
        run = await _run(db, src, job_run=jr)
        with pytest.raises(NeedsLoginError):
            await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 0
        assert realm.status == RealmStatus.LOGIN_FAILED


async def test_one_login_per_realm_per_jobrun(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(minutes=5))
        run1 = await _run(db, src, job_run=jr, status=RunStatus.COMPLETED)
        run2 = await _run(db, src, job_run=jr)
        await realm_manager.session_for_run(db, realm, run=run1)
        await realm_manager.session_for_run(db, realm, run=run2)
        assert fake_login.calls == 1
        # A later JobRun gets its own login.
        run2.status = RunStatus.COMPLETED
        await db.flush()
        jr2 = JobRun(job_id=jr.job_id, trigger="scheduled",
                     created_at=datetime.now(timezone.utc) + timedelta(seconds=1))
        db.add(jr2)
        await db.flush()
        run3 = await _run(db, src, job_run=jr2)
        await realm_manager.session_for_run(db, realm, run=run3)
        assert fake_login.calls == 2


async def test_mid_run_auth_wall_relogs_once_then_pauses(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        run.claimed_by = "worker-1"
        await db.commit()

        svc = firecrawl_mod.firecrawl_service
        out = await svc._handle_auth_wall(db, src, run.id, NeedsLoginError("wall"))
        assert fake_login.calls == 1
        assert out.status == RunStatus.PENDING
        assert out.auto_relogin_used is True
        assert out.claimed_by is None
        await db.refresh(realm)
        assert realm.status == RealmStatus.ACTIVE

        # Re-claimed and walled again: the one re-login is spent → PAUSE.
        out.status = RunStatus.RUNNING
        await db.commit()
        out = await svc._handle_auth_wall(db, src, run.id, NeedsLoginError("wall"))
        assert fake_login.calls == 1
        assert out.status == RunStatus.PAUSED
        await db.refresh(realm)
        assert realm.status == RealmStatus.EXPIRED


async def test_mid_run_auth_wall_manual_run_pauses(factory, fake_login):
    async with factory() as db:
        realm, src, _ = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=None)
        await db.commit()
        out = await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        assert fake_login.calls == 0
        assert out.status == RunStatus.PAUSED


async def test_mid_run_relogin_failure_pauses_and_keeps_login_failed(factory, monkeypatch):
    fake = FakeLogin(ok=False)
    monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        out = await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        assert fake.calls == 1
        assert out.status == RunStatus.PAUSED
        await db.refresh(realm)
        assert realm.status == RealmStatus.LOGIN_FAILED
