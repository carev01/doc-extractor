"""Tests for authenticated source extraction integration.

Verifies that extract_source aborts cleanly when a realm needs human login,
and that the run is marked FAILED with a meaningful error message.
"""
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, AuthRealm, RealmStatus
from app.models.extraction_run import RunStatus
from app.services.firecrawl import firecrawl_service
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


async def _source_with_realm(factory, realm_status):
    async with factory() as s:
        v = Vendor(name="Cohesity"); s.add(v); await s.flush()
        p = Product(name="DataProtect", vendor_id=v.id); s.add(p); await s.flush()
        r = AuthRealm(name="Cohesity", login_domain="docs.cohesity.com",
                      browserless_profile_name="realm-c", status=realm_status)
        s.add(r); await s.flush()
        src = DocumentationSource(name="DP Docs", base_url="https://docs.cohesity.com/dp",
                                  product_id=p.id, platform="generic", auth_realm_id=r.id)
        s.add(src); await s.commit(); await s.refresh(src)
        return src.id


async def test_needs_login_aborts_run(factory):
    src_id = await _source_with_realm(factory, RealmStatus.NEEDS_LOGIN)
    async with factory() as s:
        run = await firecrawl_service.extract_source(s, src_id)
        await s.commit()
    assert run.status == RunStatus.FAILED
    assert "login" in (run.error_message or "").lower()
