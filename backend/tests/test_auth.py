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


async def _admin_creates_user(client, admin_token, email, role, password="userpass123"):
    """An admin registers a downstream user (post-bootstrap path)."""
    resp = await client.post("/api/auth/register", json={
        "email": email, "display_name": email, "password": password, "role": role,
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 201, resp.text
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
    tokens = await _login(client)
    # Post-bootstrap, registration is admin-only; a duplicate email then → 409.
    resp = await client.post("/api/auth/register", json={
        "email": "admin@test.com",
        "display_name": "Another",
        "password": "anotherpass123",
        "role": "read_only",
    }, headers={"Authorization": f"Bearer {tokens['access_token']}"})
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


# ---------------------------------------------------------------------------
# Registration lockdown (bootstrap → admin-only)
# ---------------------------------------------------------------------------

async def test_status_reports_bootstrap_then_disabled(client):
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"auth_enabled": True, "needs_bootstrap": True}
    await _register(client)
    r = await client.get("/api/auth/status")
    assert r.json()["needs_bootstrap"] is False


async def test_bootstrap_first_user_forced_to_admin(client):
    """The first user is the bootstrap admin even if a lesser role is requested."""
    resp = await client.post("/api/auth/register", json={
        "email": "first@test.com", "display_name": "First",
        "password": "password123", "role": "read_only",  # ignored on bootstrap
    })
    assert resp.status_code == 201
    assert resp.json()["role"] == "admin"


async def test_register_requires_admin_after_bootstrap(client):
    await _register(client)  # bootstrap admin
    # Unauthenticated second registration is rejected (no self-service sign-up).
    resp = await client.post("/api/auth/register", json={
        "email": "evil@test.com", "display_name": "Evil",
        "password": "password123", "role": "admin",
    })
    assert resp.status_code == 403


async def test_non_admin_cannot_register_users(client):
    await _register(client)  # bootstrap admin
    admin = await _login(client)
    await _admin_creates_user(client, admin["access_token"], "rw@test.com", "read_write")
    rw = await _login(client, "rw@test.com", "userpass123")
    # A read_write user is not an admin → cannot register users.
    resp = await client.post("/api/auth/register", json={
        "email": "x@test.com", "display_name": "X",
        "password": "password123", "role": "read_only",
    }, headers={"Authorization": f"Bearer {rw['access_token']}"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# RBAC enforcement on existing routes
# ---------------------------------------------------------------------------

async def test_read_only_can_read_but_not_write(client):
    await _register(client)
    admin = await _login(client)
    await _admin_creates_user(client, admin["access_token"], "ro@test.com", "read_only")
    ro = await _login(client, "ro@test.com", "userpass123")
    h = {"Authorization": f"Bearer {ro['access_token']}"}

    assert (await client.get("/api/vendors", headers=h)).status_code == 200
    resp = await client.post("/api/vendors", json={"name": "Nope"}, headers=h)
    assert resp.status_code == 403


async def test_read_write_can_write(client):
    await _register(client)
    admin = await _login(client)
    await _admin_creates_user(client, admin["access_token"], "rw2@test.com", "read_write")
    rw = await _login(client, "rw2@test.com", "userpass123")
    resp = await client.post("/api/vendors", json={"name": "Acme"}, headers={
        "Authorization": f"Bearer {rw['access_token']}",
    })
    assert resp.status_code in (200, 201)


async def test_api_key_effective_role_restricts_writes(client):
    """An admin-owned read_only key can read but not write (effective role)."""
    await _register(client)
    admin = await _login(client)
    resp = await client.post("/api/auth/keys", json={"name": "ro-key", "role": "read_only"},
                             headers={"Authorization": f"Bearer {admin['access_token']}"})
    raw_key = resp.json()["raw_key"]
    assert (await client.get("/api/vendors", headers={"X-API-Key": raw_key})).status_code == 200
    resp = await client.post("/api/vendors", json={"name": "X"}, headers={"X-API-Key": raw_key})
    assert resp.status_code == 403


async def test_api_key_role_cannot_exceed_caller(client):
    """A non-admin cannot mint a key more powerful than their own role."""
    await _register(client)
    admin = await _login(client)
    await _admin_creates_user(client, admin["access_token"], "rw3@test.com", "read_write")
    rw = await _login(client, "rw3@test.com", "userpass123")
    resp = await client.post("/api/auth/keys", json={"name": "escalate", "role": "admin"},
                             headers={"Authorization": f"Bearer {rw['access_token']}"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Token robustness + OAuth state
# ---------------------------------------------------------------------------

async def test_malformed_token_sub_returns_401_not_500(client):
    """A token whose `sub` isn't a UUID must 401, not crash with a 500."""
    import jwt as _jwt
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    token = _jwt.encode(
        {"sub": "not-a-uuid", "type": "access", "role": "admin",
         "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())},
        settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm,
    )
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_oauth_state_roundtrip_and_rejection():
    from app.core import security
    st = security.make_oauth_state("google")
    assert security.verify_oauth_state(st, "google") is True
    # Wrong provider or tampered/garbage state → rejected.
    assert security.verify_oauth_state(st, "okta") is False
    assert security.verify_oauth_state("garbage", "google") is False


async def test_oauth_callback_rejects_invalid_state(client, monkeypatch):
    """The callback rejects a state it never issued (CSRF), before any exchange."""
    monkeypatch.setattr(settings, "auth_google_client_id", "gid")
    monkeypatch.setattr(settings, "auth_google_client_secret", "gsecret")
    resp = await client.get("/api/auth/oauth/google/callback",
                            params={"code": "x", "state": "forged-state"})
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()