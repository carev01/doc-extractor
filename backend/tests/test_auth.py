"""Tests for the authentication layer — registration, login, API keys, RBAC.

Uses the same async test harness as test_products.py: httpx.AsyncClient with
get_db overridden to point at docextractor_test. Auth is enabled by monkey-
patching settings.auth_jwt_secret.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import (
    create_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
)
from app.main import app
from app.models import User, APIKey
from app.models.user import UserRole
import app.core.auth_middleware as _auth_mw

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    """Provide an async test client with auth enabled and a fresh DB."""
    # Enable auth for tests
    monkeypatch.setattr(settings, "auth_jwt_secret", "test-secret-key-for-jwt-signing")

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # Also point the auth middleware at the test session factory so it doesn't
    # open connections on the real database during tests.
    monkeypatch.setattr(_auth_mw, "_session_factory", session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _register(client, email="admin@test.com", password="testpass123", role="admin"):
    resp = await client.post("/api/auth/register", json={
        "email": email,
        "display_name": "Test Admin",
        "password": password,
        "role": role,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client, email="admin@test.com", password="testpass123"):
    resp = await client.post("/api/auth/login", json={
        "email": email,
        "password": password,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Registration & Login
# ---------------------------------------------------------------------------

async def test_register_user(client):
    user = await _register(client)
    assert user["email"] == "admin@test.com"
    assert user["role"] == "admin"
    assert user["is_active"] is True
    assert "password" not in user
    assert "hashed_password" not in user


async def test_register_duplicate_email_fails(client):
    await _register(client)
    resp = await client.post("/api/auth/register", json={
        "email": "admin@test.com",
        "display_name": "Another",
        "password": "anotherpass123",
        "role": "read_only",
    })
    assert resp.status_code == 409


async def test_login_success(client):
    await _register(client)
    tokens = await _login(client)
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0


async def test_login_wrong_password(client):
    await _register(client)
    resp = await client.post("/api/auth/login", json={
        "email": "admin@test.com",
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


async def test_login_nonexistent_user(client):
    resp = await client.post("/api/auth/login", json={
        "email": "nobody@test.com",
        "password": "somepassword",
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

async def test_refresh_token(client):
    await _register(client)
    tokens = await _login(client)
    resp = await client.post("/api/auth/refresh", json={
        "refresh_token": tokens["refresh_token"],
    })
    assert resp.status_code == 200
    new_tokens = resp.json()
    assert new_tokens["access_token"] != tokens["access_token"]


async def test_refresh_with_access_token_fails(client):
    await _register(client)
    tokens = await _login(client)
    resp = await client.post("/api/auth/refresh", json={
        "refresh_token": tokens["access_token"],  # wrong type
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /me endpoint
# ---------------------------------------------------------------------------

async def test_get_me_with_token(client):
    await _register(client)
    tokens = await _login(client)
    resp = await client.get("/api/auth/me", headers={
        "Authorization": f"Bearer {tokens['access_token']}",
    })
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@test.com"


async def test_get_me_without_token(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_get_me_with_invalid_token(client):
    resp = await client.get("/api/auth/me", headers={
        "Authorization": "Bearer invalidtoken123",
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API Key CRUD
# ---------------------------------------------------------------------------

async def test_create_and_use_api_key(client):
    await _register(client)
    tokens = await _login(client)

    # Create an API key
    resp = await client.post("/api/auth/keys", json={
        "name": "Test Key",
        "role": "read_write",
    }, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Key"
    assert data["role"] == "read_write"
    assert data["is_active"] is True
    assert data["raw_key"].startswith("dxk_")
    raw_key = data["raw_key"]

    # Use the API key to access /me
    resp = await client.get("/api/auth/me", headers={
        "X-API-Key": raw_key,
    })
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@test.com"


async def test_list_api_keys(client):
    await _register(client)
    tokens = await _login(client)

    # Create two keys
    for name in ["Key A", "Key B"]:
        await client.post("/api/auth/keys", json={"name": name}, headers={
            "Authorization": f"Bearer {tokens['access_token']}",
        })

    resp = await client.get("/api/auth/keys", headers={
        "Authorization": f"Bearer {tokens['access_token']}",
    })
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) == 2
    # raw_key should NOT be in the list response
    assert "raw_key" not in keys[0]


async def test_revoke_api_key(client):
    await _register(client)
    tokens = await _login(client)

    resp = await client.post("/api/auth/keys", json={"name": "To Revoke"}, headers={
        "Authorization": f"Bearer {tokens['access_token']}",
    })
    key_id = resp.json()["id"]
    raw_key = resp.json()["raw_key"]

    # Revoke
    resp = await client.delete(f"/api/auth/keys/{key_id}", headers={
        "Authorization": f"Bearer {tokens['access_token']}",
    })
    assert resp.status_code == 204

    # Revoked key should no longer work
    resp = await client.get("/api/auth/me", headers={
        "X-API-Key": raw_key,
    })
    assert resp.status_code == 401


async def test_invalid_api_key(client):
    resp = await client.get("/api/auth/me", headers={
        "X-API-Key": "dxk_invalidkey123",
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth middleware — protected routes
# ---------------------------------------------------------------------------

async def test_protected_route_without_auth(client):
    """Existing /api/ routes should require authentication."""
    resp = await client.get("/api/vendors")
    assert resp.status_code == 401


async def test_protected_route_with_api_key(client):
    await _register(client)
    tokens = await _login(client)

    # Create API key
    resp = await client.post("/api/auth/keys", json={"name": "Test"}, headers={
        "Authorization": f"Bearer {tokens['access_token']}",
    })
    raw_key = resp.json()["raw_key"]

    # Access protected route with API key
    resp = await client.get("/api/vendors", headers={
        "X-API-Key": raw_key,
    })
    assert resp.status_code == 200


async def test_protected_route_with_bearer_token(client):
    await _register(client)
    tokens = await _login(client)

    resp = await client.get("/api/vendors", headers={
        "Authorization": f"Bearer {tokens['access_token']}",
    })
    assert resp.status_code == 200


async def test_health_endpoint_exempt(client):
    """Health check should not require authentication."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_auth_login_exempt(client):
    """Login endpoint should not require authentication."""
    resp = await client.post("/api/auth/login", json={
        "email": "nobody@test.com",
        "password": "wrongpassword",
    })
    # 401 is fine (bad credentials) — but NOT 401 "Not authenticated" from middleware
    # The fact we get here means the middleware let the request through
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"


# ---------------------------------------------------------------------------
# Auth disabled mode
# ---------------------------------------------------------------------------

async def test_auth_disabled_allows_access(client, monkeypatch):
    """When auth_jwt_secret is empty, all routes should be accessible."""
    monkeypatch.setattr(settings, "auth_jwt_secret", "")

    resp = await client.get("/api/vendors")
    assert resp.status_code == 200  # No auth required when disabled