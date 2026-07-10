"""Per-vendor (row-level) authorization + user/permission/key management tests.

Exercises the full stack through the ASGI app with auth enabled: middleware
method-RBAC + per-vendor grants, admin-only management endpoints, the vendor
allow-list (ungranted vendors are 404-invisible), key rotation, and password
change.
"""
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
import app.core.auth_middleware as _auth_mw

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_jwt_secret", "test-secret-key-for-jwt-signing")
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(_auth_mw, "_session_factory", factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


async def _bootstrap_admin(client):
    r = await client.post("/api/auth/register", json={
        "email": "admin@t.com", "display_name": "Admin", "password": "adminpass1", "role": "admin"})
    assert r.status_code == 201 and r.json()["role"] == "admin"
    tok = (await client.post("/api/auth/login", json={"email": "admin@t.com", "password": "adminpass1"})).json()
    return tok["access_token"]


async def _make_user(client, admin_h, email, role):
    r = await client.post("/api/auth/register", json={
        "email": email, "display_name": email, "password": "userpass1", "role": role}, headers=admin_h)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    tok = (await client.post("/api/auth/login", json={"email": email, "password": "userpass1"})).json()["access_token"]
    return uid, tok


async def _vendor_with_source(client, admin_h, name):
    v = (await client.post("/api/vendors", json={"name": name}, headers=admin_h)).json()
    p = (await client.post("/api/products", json={"vendor_id": v["id"], "name": name + " P"}, headers=admin_h)).json()
    s = (await client.post("/api/sources", json={
        "product_id": p["id"], "name": name + " S", "base_url": f"https://{name}.example.com"},
        headers=admin_h)).json()
    return v["id"], p["id"], s["id"]


# ── Vendor allow-list visibility ────────────────────────────────────────────

async def test_ungranted_vendors_are_invisible(client):
    admin = _bearer(await _bootstrap_admin(client))
    v1, p1, s1 = await _vendor_with_source(client, admin, "v1")
    v2, p2, s2 = await _vendor_with_source(client, admin, "v2")
    _, rw = await _make_user(client, admin, "rw@t.com", "read_write")
    H = _bearer(rw)

    # No grants yet → sees no vendors, and each is 404 (invisible, not 403).
    assert (await client.get("/api/vendors", headers=H)).json()["total"] == 0
    assert (await client.get(f"/api/vendors/{v1}", headers=H)).status_code == 404
    assert (await client.get(f"/api/sources/{s1}", headers=H)).status_code == 404
    # Admin sees both.
    assert (await client.get("/api/vendors", headers=admin)).json()["total"] == 2


async def test_grant_scopes_visibility_and_writes(client):
    admin = _bearer(await _bootstrap_admin(client))
    v1, p1, s1 = await _vendor_with_source(client, admin, "v1")
    v2, p2, s2 = await _vendor_with_source(client, admin, "v2")
    rw_id, rw = await _make_user(client, admin, "rw@t.com", "read_write")
    H = _bearer(rw)

    # Grant read_write on v1, read_only on v2.
    r = await client.put(f"/api/auth/users/{rw_id}/vendor-permissions", json={"permissions": [
        {"vendor_id": v1, "level": "read_write"},
        {"vendor_id": v2, "level": "read_only"},
    ]}, headers=admin)
    assert r.status_code == 200

    # Both vendors now visible.
    vids = {v["id"] for v in (await client.get("/api/vendors", headers=H)).json()["vendors"]}
    assert vids == {v1, v2}
    # Sources list scoped to granted vendors.
    sids = {s["id"] for s in (await client.get("/api/sources", headers=H)).json()["sources"]}
    assert sids == {s1, s2}

    # Write allowed on v1 (read_write grant)…
    assert (await client.post("/api/products", json={"vendor_id": v1, "name": "new"}, headers=H)).status_code == 201
    # …but denied on v2 (read_only grant) → 403.
    assert (await client.post("/api/products", json={"vendor_id": v2, "name": "no"}, headers=H)).status_code == 403
    # Reading v2 is fine.
    assert (await client.get(f"/api/sources/{s2}", headers=H)).status_code == 200


async def test_global_read_only_cannot_write_even_with_grant(client):
    admin = _bearer(await _bootstrap_admin(client))
    v1, p1, s1 = await _vendor_with_source(client, admin, "v1")
    ro_id, ro = await _make_user(client, admin, "ro@t.com", "read_only")
    # Grant read_only on v1 (can't grant read_write to a global read_only user).
    await client.put(f"/api/auth/users/{ro_id}/vendor-permissions", json={"permissions": [
        {"vendor_id": v1, "level": "read_only"}]}, headers=admin)
    H = _bearer(ro)
    assert (await client.get(f"/api/sources/{s1}", headers=H)).status_code == 200   # read ok
    assert (await client.post("/api/products", json={"vendor_id": v1, "name": "x"}, headers=H)).status_code == 403


async def test_cannot_grant_read_write_to_global_read_only(client):
    admin = _bearer(await _bootstrap_admin(client))
    v1, _, _ = await _vendor_with_source(client, admin, "v1")
    ro_id, _ = await _make_user(client, admin, "ro@t.com", "read_only")
    r = await client.put(f"/api/auth/users/{ro_id}/vendor-permissions", json={"permissions": [
        {"vendor_id": v1, "level": "read_write"}]}, headers=admin)
    assert r.status_code == 400


# ── Admin-only surfaces ─────────────────────────────────────────────────────

async def test_vendor_creation_is_admin_only(client):
    admin = _bearer(await _bootstrap_admin(client))
    _, rw = await _make_user(client, admin, "rw@t.com", "read_write")
    assert (await client.post("/api/vendors", json={"name": "nope"}, headers=_bearer(rw))).status_code == 403


async def test_duplicate_vendor_name_conflict(client):
    admin = _bearer(await _bootstrap_admin(client))
    await client.post("/api/vendors", json={"name": "Acme"}, headers=admin)
    r = await client.post("/api/vendors", json={"name": "Acme"}, headers=admin)
    assert r.status_code == 409


async def test_user_management_requires_admin(client):
    admin = _bearer(await _bootstrap_admin(client))
    _, rw = await _make_user(client, admin, "rw@t.com", "read_write")
    assert (await client.get("/api/auth/users", headers=_bearer(rw))).status_code == 403
    assert (await client.get("/api/auth/users", headers=admin)).status_code == 200
    # jobs + auth-realms are admin-only too.
    assert (await client.get("/api/jobs", headers=_bearer(rw))).status_code == 403
    assert (await client.get("/api/auth-realms", headers=_bearer(rw))).status_code == 403


async def test_admin_cannot_lock_self_out(client):
    admin_tok = await _bootstrap_admin(client)
    admin = _bearer(admin_tok)
    me = (await client.get("/api/auth/me", headers=admin)).json()
    # demote self → 400; deactivate self → 400.
    assert (await client.patch(f"/api/auth/users/{me['id']}", json={"role": "read_only"}, headers=admin)).status_code == 400
    assert (await client.patch(f"/api/auth/users/{me['id']}", json={"is_active": False}, headers=admin)).status_code == 400


# ── Keys + password ─────────────────────────────────────────────────────────

async def test_key_rotate_and_admin_revoke(client):
    admin = _bearer(await _bootstrap_admin(client))
    rw_id, rw = await _make_user(client, admin, "rw@t.com", "read_write")
    H = _bearer(rw)
    created = (await client.post("/api/auth/keys", json={"name": "k", "role": "read_only"}, headers=H)).json()
    old_raw, kid = created["raw_key"], created["id"]

    rotated = (await client.post(f"/api/auth/keys/{kid}/rotate", headers=H)).json()
    assert rotated["raw_key"] != old_raw
    # Old key no longer authenticates.
    assert (await client.get("/api/auth/me", headers={"X-API-Key": old_raw})).status_code == 401
    # New key works.
    assert (await client.get("/api/auth/me", headers={"X-API-Key": rotated["raw_key"]})).status_code == 200

    # Admin sees the key in the oversight list and can revoke it.
    all_keys = (await client.get("/api/auth/admin/keys", headers=admin)).json()
    assert any(k["user_email"] == "rw@t.com" for k in all_keys)
    assert (await client.delete(f"/api/auth/keys/{rotated['id']}", headers=admin)).status_code == 204
    assert (await client.get("/api/auth/me", headers={"X-API-Key": rotated["raw_key"]})).status_code == 401


async def test_change_password(client):
    admin = _bearer(await _bootstrap_admin(client))
    _, rw = await _make_user(client, admin, "rw@t.com", "read_write")
    H = _bearer(rw)
    # wrong current → 401
    assert (await client.post("/api/auth/change-password", json={
        "current_password": "wrong", "new_password": "brandnew1"}, headers=H)).status_code == 401
    # correct → 204, and the new password logs in
    assert (await client.post("/api/auth/change-password", json={
        "current_password": "userpass1", "new_password": "brandnew1"}, headers=H)).status_code == 204
    assert (await client.post("/api/auth/login", json={"email": "rw@t.com", "password": "brandnew1"})).status_code == 200


async def test_admin_downgraded_key_sees_all_at_capped_level(client):
    """An admin-owned read_only/read_write key sees every vendor (admin owner),
    but writes are capped by the key's role."""
    admin = _bearer(await _bootstrap_admin(client))
    v1, p1, s1 = await _vendor_with_source(client, admin, "v1")

    ro_key = (await client.post("/api/auth/keys", json={"name": "ro", "role": "read_only"}, headers=admin)).json()["raw_key"]
    KH = {"X-API-Key": ro_key}
    # Sees the vendor (not an empty allow-list) and can read it…
    assert (await client.get("/api/vendors", headers=KH)).json()["total"] == 1
    assert (await client.get(f"/api/sources/{s1}", headers=KH)).status_code == 200
    # …but a read_only key cannot write, and cannot do admin-only ops.
    assert (await client.post("/api/products", json={"vendor_id": v1, "name": "x"}, headers=KH)).status_code == 403
    assert (await client.get("/api/auth/users", headers=KH)).status_code == 403

    rw_key = (await client.post("/api/auth/keys", json={"name": "rw", "role": "read_write"}, headers=admin)).json()["raw_key"]
    KH2 = {"X-API-Key": rw_key}
    assert (await client.post("/api/products", json={"vendor_id": v1, "name": "y"}, headers=KH2)).status_code == 201
