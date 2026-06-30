import io
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(tmp_path):
    settings.pdf_dir = str(tmp_path)
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _product(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.commit()
        return p.id


async def test_create_pdf_source_from_url(client):
    c, factory = client
    pid = await _product(factory)
    resp = await c.post("/api/sources/pdf", data={
        "product_id": str(pid), "name": "Spec", "pdf_url": "https://x/doc.pdf",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["source_type"] == "pdf"
    assert body["base_url"] == "https://x/doc.pdf"


async def _realm(factory) -> uuid.UUID:
    from app.models.auth_realm import AuthRealm
    async with factory() as s:
        r = AuthRealm(name="R", login_domain="x.com", auth_type="form",
                      browserless_profile_name="")
        s.add(r); await s.commit()
        return r.id


async def test_create_pdf_source_from_url_persists_auth_realm(client):
    c, factory = client
    pid = await _product(factory)
    rid = await _realm(factory)
    resp = await c.post("/api/sources/pdf", data={
        "product_id": str(pid), "name": "Spec", "pdf_url": "https://x/doc.pdf",
        "auth_realm_id": str(rid),
    })
    assert resp.status_code == 201
    assert resp.json()["auth_realm_id"] == str(rid)


async def test_upload_pdf_stores_file_and_sets_marker(client, tmp_path):
    c, factory = client
    pid = await _product(factory)
    files = {"file": ("d.pdf", io.BytesIO(b"%PDF-1.4 hi"), "application/pdf")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Up"}, files=files)
    assert resp.status_code == 201
    body = resp.json()
    sid = body["id"]
    assert body["base_url"] == f"file://{sid}.pdf"
    assert os.path.exists(os.path.join(str(tmp_path), f"{sid}.pdf"))


async def test_blank_name_is_422(client):
    c, factory = client
    pid = await _product(factory)
    resp = await c.post("/api/sources/pdf", data={
        "product_id": str(pid), "name": "   ", "pdf_url": "https://x/doc.pdf",
    })
    assert resp.status_code == 422


async def test_non_pdf_upload_is_415(client):
    c, factory = client
    pid = await _product(factory)
    files = {"file": ("d.txt", io.BytesIO(b"hi"), "text/plain")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Bad"}, files=files)
    assert resp.status_code == 415


async def test_edit_from_url_pdf_relocates_and_clears_template(client):
    c, factory = client
    pid = await _product(factory)
    created = (await c.post("/api/sources/pdf", data={
        "product_id": str(pid), "name": "Spec", "pdf_url": "https://old/7_4/74_UserGuide.pdf",
    })).json()
    sid = created["id"]
    # Give it a (now mismatched) version template, as a versioned source would have.
    await c.patch(f"/api/sources/{sid}", json={"url_template": "https://old/7_4/74_UserGuide.pdf"})

    # Relocate to a wholly different host/filename — the kind a {version} template
    # can't represent.
    resp = await c.patch(f"/api/sources/{sid}", json={
        "base_url": "https://new-cdn/guides/cohesity-user-guide-7-5.pdf",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_url"] == "https://new-cdn/guides/cohesity-user-guide-7-5.pdf"
    # The stale template is cleared so a later version bump can't resurrect the old URL.
    assert body["url_template"] is None


async def test_edit_url_rejects_non_http(client):
    c, factory = client
    pid = await _product(factory)
    sid = (await c.post("/api/sources/pdf", data={
        "product_id": str(pid), "name": "Spec", "pdf_url": "https://x/doc.pdf",
    })).json()["id"]
    resp = await c.patch(f"/api/sources/{sid}", json={"base_url": "file://evil.pdf"})
    assert resp.status_code == 422


async def test_edit_url_rejected_on_upload_origin_pdf(client):
    c, factory = client
    pid = await _product(factory)
    files = {"file": ("d.pdf", io.BytesIO(b"%PDF-1.4 hi"), "application/pdf")}
    sid = (await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Up"}, files=files)).json()["id"]
    # An upload-origin (file://) PDF must use Replace file, not a URL edit.
    resp = await c.patch(f"/api/sources/{sid}", json={"base_url": "https://x/doc.pdf"})
    assert resp.status_code == 409


async def test_oversize_upload_is_413(client):
    c, factory = client
    settings.pdf_max_upload_bytes = 10
    pid = await _product(factory)
    files = {"file": ("d.pdf", io.BytesIO(b"%PDF-1.4 " + b"x" * 100), "application/pdf")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Big"}, files=files)
    assert resp.status_code == 413
    settings.pdf_max_upload_bytes = 100 * 1024 * 1024
