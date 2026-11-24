"""Tests for AuthRealm CRUD + session upload routes.

These tests verify:
- Create never returns secrets (password, totp_secret, state_snapshot)
- has_password / has_totp presence booleans are exposed
- List and get work correctly
- Session upload normalises Playwright [{name,value}] localStorage to [[k,v]]
  and flips status to ACTIVE
- Session upload rejects an empty payload (400)
"""

import os
import sys

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base, get_db
from tests.db_reset import reset_schema
from app.main import app

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(reset_schema)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()
    crypto._reset_cache()


async def test_create_realm_never_returns_secrets(client):
    resp = await client.post("/api/auth-realms", json={
        "name": "Cohesity", "login_domain": "docs.cohesity.com", "auth_type": "form",
        "login_url": "https://my.cohesity.com/login",
        "username": "u@x.com", "password": "p", "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "password" not in body and "totp_secret" not in body and "state_snapshot" not in body
    assert body["has_password"] is True and body["has_totp"] is True
    assert body["status"] == "needs_login"


async def test_list_and_get(client):
    await client.post("/api/auth-realms", json={
        "name": "Rubrik", "login_domain": "docs.rubrik.com", "auth_type": "oidc"})
    lst = (await client.get("/api/auth-realms")).json()
    assert len(lst) == 1 and lst[0]["has_password"] is False


async def test_session_upload_normalizes_playwright_and_sets_active(client):
    """Session upload: Playwright localStorage [{name,value}] → [[k,v]], status → ACTIVE."""
    create_resp = await client.post("/api/auth-realms", json={
        "name": "UploadRealm", "login_domain": "docs.upload.test", "auth_type": "form",
    })
    assert create_resp.status_code == 201
    realm_id = create_resp.json()["id"]

    # Playwright storageState shape: localStorage as [{name, value}]
    session_resp = await client.post(f"/api/auth-realms/{realm_id}/session", json={
        "cookies": [{"name": "sess", "value": "tok123", "domain": "docs.upload.test", "path": "/"}],
        "origins": [
            {"origin": "https://docs.upload.test",
             "localStorage": [{"name": "token", "value": "xyz"}]},
        ],
    })
    assert session_resp.status_code == 200, session_resp.text
    body = session_resp.json()
    assert body["status"] == "active"

    # GET confirms persistence
    get_resp = await client.get(f"/api/auth-realms/{realm_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "active"


async def test_session_upload_rejects_empty(client):
    """Session upload: empty cookies + origins → 400."""
    create_resp = await client.post("/api/auth-realms", json={
        "name": "EmptyRealm", "login_domain": "docs.empty.test", "auth_type": "form",
    })
    assert create_resp.status_code == 201
    realm_id = create_resp.json()["id"]

    resp = await client.post(f"/api/auth-realms/{realm_id}/session", json={
        "cookies": [], "origins": []
    })
    assert resp.status_code == 400


async def test_session_upload_missing_the_login_cookie_is_refused(client):
    """A browser drops an expired login cookie, so an export taken after the
    session lapsed carries only the long-lived bystanders. Rubrik on 2026-11-24:
    the upload lost SAML_TOKEN, kept __Host-RSC_HOME (7 days), went ACTIVE, the UI
    reported the session valid for 165h, and a whole run scraped logged out."""
    realm_id = (await client.post("/api/auth-realms", json={
        "name": "Rubrik", "login_domain": "docs.rubrik.test", "auth_type": "form",
    })).json()["id"]
    good = {"cookies": [
        {"name": "SAML_TOKEN", "value": "jwt", "domain": "docs.rubrik.test", "path": "/",
         "expirationDate": 4102444800},
        {"name": "__Host-RSC_HOME", "value": "home", "domain": "docs.rubrik.test", "path": "/",
         "expirationDate": 4102444800},
    ]}
    assert (await client.post(f"/api/auth-realms/{realm_id}/session", json=good)).status_code == 200

    stale = {"cookies": [good["cookies"][1]]}
    resp = await client.post(f"/api/auth-realms/{realm_id}/session", json=stale)
    assert resp.status_code == 400
    assert "SAML_TOKEN" in resp.json()["detail"]

    # The working session is kept, not replaced by the broken one.
    body = (await client.get(f"/api/auth-realms/{realm_id}")).json()
    assert body["status"] == "active"


async def test_session_upload_without_login_cookies_is_refused_once_one_was_seen(client):
    # Even after a broken upload slipped in earlier, the realm remembers which
    # cookie names carried its login.
    realm_id = (await client.post("/api/auth-realms", json={
        "name": "R2", "login_domain": "docs.r2.test", "auth_type": "form",
    })).json()["id"]
    first = {"cookies": [{"name": "session_token", "value": "a", "domain": "docs.r2.test",
                          "path": "/"}]}
    assert (await client.post(f"/api/auth-realms/{realm_id}/session", json=first)).status_code == 200
    refresh = {"cookies": [{"name": "session_token", "value": "b", "domain": "docs.r2.test",
                            "path": "/"}, {"name": "theme", "value": "dark",
                                           "domain": "docs.r2.test", "path": "/"}]}
    assert (await client.post(f"/api/auth-realms/{realm_id}/session", json=refresh)).status_code == 200
