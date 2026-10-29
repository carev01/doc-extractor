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


# --- Fix round 1 -----------------------------------------------------------

async def test_guard_blocks_second_login_on_expired_realm(factory, fake_login):
    """Sibling A logged in this firing, then walled → realm EXPIRED. Sibling B must
    not log in again (ensure_session's own fallback would) — it fails instead."""
    async with factory() as db:
        realm, src, jr = await _setup(db, status=RealmStatus.EXPIRED,
                                      expires_in=timedelta(minutes=5))
        realm.last_login_at = jr.created_at + timedelta(minutes=1)
        await db.flush()
        run = await _run(db, src, job_run=jr)
        with pytest.raises(NeedsLoginError):
            await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 0


async def test_guard_returns_usable_snapshot_when_already_logged_in(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(minutes=5))
        realm.last_login_at = jr.created_at + timedelta(minutes=1)
        await db.flush()
        run = await _run(db, src, job_run=jr)
        state = await realm_manager.session_for_run(db, realm, run=run)
        assert fake_login.calls == 0
        assert state == realm.state_snapshot


async def test_raw_http_expiry_relogs_job_run_once_then_pauses(factory, fake_login):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        run.claimed_by = "worker-1"
        await db.commit()

        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "requeue"
        assert fake_login.calls == 1
        await db.refresh(run)
        assert run.auto_relogin_used is True
        await db.refresh(realm)
        assert realm.status == RealmStatus.ACTIVE

        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "pause"
        assert fake_login.calls == 1
        await db.refresh(realm)
        assert realm.status == RealmStatus.EXPIRED


async def test_raw_http_expiry_manual_run_pauses(factory, fake_login):
    async with factory() as db:
        realm, src, _ = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=None)
        await db.commit()
        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "pause"
        assert fake_login.calls == 0


async def test_raw_http_relogin_failure_pauses_and_keeps_login_failed(factory, monkeypatch):
    fake = FakeLogin(ok=False)
    monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "pause"
        assert fake.calls == 1
        await db.refresh(realm)
        assert realm.status == RealmStatus.LOGIN_FAILED


async def test_requeue_signal_requeues_run_like_resume():
    """extract_source's RunControlSignal handler maps 'requeue' to PENDING."""
    from types import SimpleNamespace
    run = SimpleNamespace(status=RunStatus.RUNNING, control="x", claimed_by="w",
                          claimed_at=1, heartbeat_at=1, error_message="e",
                          completed_at=None)
    firecrawl_mod._requeue_like_resume(run)
    assert run.status == RunStatus.PENDING
    assert run.control is None and run.claimed_by is None
    assert run.claimed_at is None and run.heartbeat_at is None
    assert run.error_message is None


@pytest_asyncio.fixture
async def client(factory):
    from httpx import ASGITransport, AsyncClient
    from app.core.database import get_db
    from app.main import app

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize("field,value", [
    ("username", "new-user"), ("password", "new-secret"), ("totp_secret", "JBSWY3DPEHPK3PXP"),
])
async def test_credential_change_clears_login_failed(factory, client, field, value):
    async with factory() as db:
        realm, _, _ = await _setup(db, status=RealmStatus.LOGIN_FAILED)
        await db.commit()
        rid = realm.id
    resp = await client.patch(f"/api/auth-realms/{rid}", json={field: value})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "needs_login"


async def test_non_credential_edit_keeps_login_failed(factory, client):
    async with factory() as db:
        realm, _, _ = await _setup(db, status=RealmStatus.LOGIN_FAILED)
        await db.commit()
        rid = realm.id
    resp = await client.patch(f"/api/auth-realms/{rid}",
                              json={"name": "renamed", "password": "secret"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "login_failed"


# --- Final review: a Cancel clicked during the re-login wins ----------------

class CancellingLogin(FakeLogin):
    """A scripted login during which the user clicks Cancel (the API commits
    ``control='cancel'`` from its own session while the login is in flight)."""

    def __init__(self, factory, run_id):
        super().__init__()
        self.factory, self.run_id = factory, run_id

    async def __call__(self, db, realm):
        async with self.factory() as other:
            r = await other.get(ExtractionRun, self.run_id)
            r.control = "cancel"
            await other.commit()
        await super().__call__(db, realm)


async def test_cancel_during_mid_run_relogin_cancels_instead_of_requeue(factory, monkeypatch):
    from app.models import TocCheckpoint
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        db.add(TocCheckpoint(source_id=src.id))
        await db.commit()
        fake = CancellingLogin(factory, run.id)
        monkeypatch.setattr(realm_manager, "run_scripted_login", fake)

        out = await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        assert fake.calls == 1
        assert out.status == RunStatus.CANCELLED
        assert out.completed_at is not None
    async with factory() as fresh:
        row = await fresh.get(ExtractionRun, run.id)
        assert row.status == RunStatus.CANCELLED and row.control is None
        from sqlalchemy import select as _sel
        assert (await fresh.execute(
            _sel(TocCheckpoint).where(TocCheckpoint.source_id == src.id))).first() is None


async def test_cancel_during_raw_http_relogin_raises_cancel(factory, monkeypatch):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        fake = CancellingLogin(factory, run.id)
        monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "cancel"
        assert fake.calls == 1


# --- Deferred minors: pause during re-login; cancel during a failed re-login --

class ControlDuringLogin(FakeLogin):
    """A scripted login during which the user clicks Pause/Cancel (committed by
    the API from its own session while the login is in flight)."""

    def __init__(self, factory, run_id, control, ok=True):
        super().__init__(ok=ok)
        self.factory, self.run_id, self.control = factory, run_id, control

    async def __call__(self, db, realm):
        async with self.factory() as other:
            r = await other.get(ExtractionRun, self.run_id)
            r.control = self.control
            await other.commit()
        await super().__call__(db, realm)


async def test_pause_during_mid_run_relogin_pauses_instead_of_requeue(factory, monkeypatch):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        fake = ControlDuringLogin(factory, run.id, "pause")
        monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
        out = await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        assert fake.calls == 1
        assert out.status == RunStatus.PAUSED
    async with factory() as fresh:
        row = await fresh.get(ExtractionRun, run.id)
        assert row.status == RunStatus.PAUSED and row.control is None
        assert (await fresh.get(AuthRealm, realm.id)).status == RealmStatus.ACTIVE


async def test_pause_during_raw_http_relogin_raises_pause(factory, monkeypatch):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        fake = ControlDuringLogin(factory, run.id, "pause")
        monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "pause"


async def test_cancel_during_failed_mid_run_relogin_cancels(factory, monkeypatch):
    from app.models import TocCheckpoint
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        db.add(TocCheckpoint(source_id=src.id))
        await db.commit()
        fake = ControlDuringLogin(factory, run.id, "cancel", ok=False)
        monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
        out = await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        assert fake.calls == 1
        assert out.status == RunStatus.CANCELLED and out.completed_at is not None
    async with factory() as fresh:
        row = await fresh.get(ExtractionRun, run.id)
        assert row.status == RunStatus.CANCELLED and row.control is None
        assert (await fresh.get(AuthRealm, realm.id)).status == RealmStatus.LOGIN_FAILED
        from sqlalchemy import select as _sel
        assert (await fresh.execute(
            _sel(TocCheckpoint).where(TocCheckpoint.source_id == src.id))).first() is None


async def test_cancel_during_failed_raw_http_relogin_raises_cancel(factory, monkeypatch):
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        fake = ControlDuringLogin(factory, run.id, "cancel", ok=False)
        monkeypatch.setattr(realm_manager, "run_scripted_login", fake)
        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "cancel"
        await db.refresh(realm)
        assert realm.status == RealmStatus.LOGIN_FAILED


@pytest.mark.parametrize("control,expected", [
    (None, "requeue"), ("cancel", "cancel"), ("pause", "pause"),
])
async def test_relogin_outcome_rereads_control(factory, control, expected):
    """The extract_source handler re-checks a 'requeue' with this after the
    re-login's commit: a Pause/Cancel that landed since still wins."""
    async with factory() as db:
        realm, src, jr = await _setup(db)
        run = await _run(db, src, job_run=jr)
        await db.commit()
        async with factory() as other:
            r = await other.get(ExtractionRun, run.id)
            r.control = control
            await other.commit()
        assert await firecrawl_mod._relogin_outcome(db, run) == expected


# --- B7: the pause notification names the right fix --------------------------

@pytest.fixture
def notes(monkeypatch):
    sent: list[str] = []

    async def _capture(title, body, **kw):
        sent.append(body)
    monkeypatch.setattr(firecrawl_mod, "notify", _capture)
    return sent


async def test_pause_after_rejected_relogin_says_fix_credentials(factory, monkeypatch, notes):
    monkeypatch.setattr(realm_manager, "run_scripted_login", FakeLogin(ok=False))
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=jr)
        await db.commit()
        await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        run2 = await _run(db, src, job_run=None)
        await db.commit()
        with pytest.raises(firecrawl_mod.RunControlSignal):
            await firecrawl_mod._pause_for_expiry(db, src, realm, run2.id)
    assert len(notes) == 2
    for body in notes:
        assert "credentials" in body and "fresh cookie" not in body


async def test_pause_of_cookie_realm_says_upload_cookie(factory, fake_login, notes):
    async with factory() as db:
        realm, src, _ = await _setup(db, expires_in=timedelta(hours=3), creds=False)
        run = await _run(db, src, job_run=None)
        await db.commit()
        await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
    assert len(notes) == 1 and "fresh cookie" in notes[0]


# --- Fix round 1: one pre-run login ATTEMPT per realm per JobRun --------------

async def test_browserless_outage_logs_in_once_per_firing(factory, monkeypatch):
    """An infrastructure failure leaves the realm EXPIRED (not LOGIN_FAILED), so
    the once-per-firing guard must key on the attempt, not on a success: the
    sibling run of the same JobRun fails without a second login."""
    from unittest.mock import AsyncMock
    from app.services.browserless import BrowserlessError
    client = AsyncMock()
    client.run_login.side_effect = BrowserlessError("browserless 503")
    monkeypatch.setattr(realm_manager, "browserless_client", client)
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(minutes=5))
        run1 = await _run(db, src, job_run=jr, status=RunStatus.RUNNING)
        await db.commit()
        rid, r1, jid = realm.id, run1.id, jr.id
    async with factory() as db:  # sibling 1 (its own worker session)
        realm, run1 = await db.get(AuthRealm, rid), await db.get(ExtractionRun, r1)
        with pytest.raises(NeedsLoginError):
            await realm_manager.session_for_run(db, realm, run=run1)
        run1.status = RunStatus.FAILED
        await db.commit()
        assert realm.status == RealmStatus.EXPIRED
    async with factory() as db:  # sibling 2
        realm = await db.get(AuthRealm, rid)
        src = await db.get(DocumentationSource, src.id)
        run2 = await _run(db, src, job_run=await db.get(JobRun, jid))
        with pytest.raises(NeedsLoginError):
            await realm_manager.session_for_run(db, realm, run=run2)
    assert client.run_login.await_count == 1


async def test_next_firing_retries_after_an_outage(factory, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.browserless import BrowserlessError
    client = AsyncMock()
    client.run_login.side_effect = BrowserlessError("browserless 503")
    monkeypatch.setattr(realm_manager, "browserless_client", client)
    async with factory() as db:
        realm, src, jr = await _setup(db, expires_in=timedelta(minutes=5))
        run1 = await _run(db, src, job_run=jr)
        with pytest.raises(NeedsLoginError):
            await realm_manager.session_for_run(db, realm, run=run1)
        run1.status = RunStatus.FAILED
        jr2 = JobRun(job_id=jr.job_id, trigger="scheduled",
                     created_at=datetime.now(timezone.utc) + timedelta(seconds=1))
        db.add(jr2)
        await db.flush()
        run2 = await _run(db, src, job_run=jr2)
        with pytest.raises(NeedsLoginError):
            await realm_manager.session_for_run(db, realm, run=run2)
    assert client.run_login.await_count == 2


# --- Fix round 1: the run row is not locked while notifying -------------------

class LockProbe:
    """A notify() stand-in that, from another session, tries to write the run
    row as the API's Pause/Cancel would — with a short lock_timeout, so a lock
    still held by the worker shows up as a failure instead of a hang."""

    def __init__(self, factory, run_id):
        self.factory, self.run_id, self.results = factory, run_id, []

    async def __call__(self, *a, **kw):
        from sqlalchemy import text
        async with self.factory() as other:
            try:
                await other.execute(text("SET LOCAL lock_timeout = '500ms'"))
                await other.execute(
                    text("UPDATE extraction_runs SET error_message = 'probe' WHERE id = :id"),
                    {"id": self.run_id})
                await other.rollback()
                self.results.append("free")
            except Exception as exc:  # LockNotAvailable
                await other.rollback()
                self.results.append(f"locked: {type(exc).__name__}")


async def test_auth_wall_pause_does_not_hold_run_lock_while_notifying(factory, fake_login, monkeypatch):
    async with factory() as db:
        realm, src, _ = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=None)
        await db.commit()
        probe = LockProbe(factory, run.id)
        monkeypatch.setattr(firecrawl_mod, "notify", probe)
        out = await firecrawl_mod.firecrawl_service._handle_auth_wall(
            db, src, run.id, NeedsLoginError("wall"))
        assert out.status == RunStatus.PAUSED
    assert probe.results == ["free"]
    async with factory() as fresh:  # the PAUSED state was committed before notify
        assert (await fresh.get(ExtractionRun, run.id)).status == RunStatus.PAUSED


async def test_raw_http_pause_does_not_hold_run_lock_while_notifying(factory, fake_login, monkeypatch):
    async with factory() as db:
        realm, src, _ = await _setup(db, expires_in=timedelta(hours=3))
        run = await _run(db, src, job_run=None)
        await db.commit()
        probe = LockProbe(factory, run.id)
        monkeypatch.setattr(firecrawl_mod, "notify", probe)
        with pytest.raises(firecrawl_mod.RunControlSignal) as ei:
            await firecrawl_mod._pause_for_expiry(db, src, realm, run.id)
        assert ei.value.action == "pause"
    assert probe.results == ["free"]
