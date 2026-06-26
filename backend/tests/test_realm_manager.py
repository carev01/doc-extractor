import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base
from app.models import AuthRealm, RealmStatus
from app.services.auth import realm_manager

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    crypto._reset_cache()


async def _add(factory, **kw):
    base = dict(name="X", login_domain="docs.x.com", auth_type="form",
                browserless_profile_name="realm-x", status=RealmStatus.NEEDS_LOGIN)
    base.update(kw)
    async with factory() as s:
        r = AuthRealm(**base); s.add(r); await s.commit(); await s.refresh(r)
        return r


async def test_active_realm_short_circuits(factory, monkeypatch):
    snap = {"cookies": [{"name": "sid", "value": "x"}]}
    r = await _add(factory, status=RealmStatus.ACTIVE,
                   state_snapshot=snap,
                   last_login_at=datetime.now(timezone.utc))
    login = AsyncMock()
    monkeypatch.setattr(realm_manager, "run_scripted_login", login)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        result = await realm_manager.ensure_session(s, realm)
    assert result == snap
    login.assert_not_awaited()


async def test_needs_login_without_creds_raises(factory):
    r = await _add(factory, status=RealmStatus.NEEDS_LOGIN)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        with pytest.raises(realm_manager.NeedsLoginError):
            await realm_manager.ensure_session(s, realm)


async def test_needs_login_with_creds_runs_scripted(factory, monkeypatch):
    r = await _add(factory, status=RealmStatus.NEEDS_LOGIN, username="u", password="p")
    snap = {"cookies": []}

    async def fake_login(db, realm):
        realm.status = RealmStatus.ACTIVE
        realm.state_snapshot = snap

    monkeypatch.setattr(realm_manager, "run_scripted_login", fake_login)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        result = await realm_manager.ensure_session(s, realm)
    assert result == snap


async def test_run_scripted_login_stores_snapshot(factory, monkeypatch):
    r = await _add(factory, status=RealmStatus.NEEDS_LOGIN, username="u", password="p")
    fake_client = AsyncMock()
    fake_client.run_login.return_value = {
        "ok": True, "cookieCount": 3, "finalUrl": "https://docs.x.com/",
        "state": {"cookies": [{"name": "sid", "value": "abc"}]},
    }
    monkeypatch.setattr(realm_manager, "browserless_client", fake_client)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        await realm_manager.run_scripted_login(s, realm)
        await s.commit()
        assert realm.status == RealmStatus.ACTIVE
        assert realm.state_snapshot["cookies"][0]["name"] == "sid"
