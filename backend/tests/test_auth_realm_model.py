import os
import sys
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base
from app.models import AuthRealm, RealmStatus

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


async def test_secrets_persist_encrypted_and_read_back(factory):
    rid = uuid.uuid4()
    async with factory() as s:
        s.add(AuthRealm(
            id=rid, name="Cohesity", login_domain="docs.cohesity.com",
            auth_type="form", password="s3cret", totp_secret="JBSWY3DPEHPK3PXP",
            browserless_profile_name=f"realm-{rid}", status=RealmStatus.NEEDS_LOGIN,
        ))
        await s.commit()
    async with factory() as s:
        realm = (await s.execute(select(AuthRealm).where(AuthRealm.id == rid))).scalar_one()
        assert realm.password == "s3cret"
        assert realm.totp_secret == "JBSWY3DPEHPK3PXP"
        assert realm.status == RealmStatus.NEEDS_LOGIN
