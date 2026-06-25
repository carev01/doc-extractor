"""Tests for versioned-URL source routes.

Exercises:
- POST /api/sources/{id}/detect-version-token
- url_template field on SourceCreate / SourceUpdate (base_url resolution when
  the product has a version, passthrough when it doesn't)

Uses the same httpx.AsyncClient + get_db-override harness as test_products.py.
"""

import os
import sys
import uuid

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
from app.main import app
from app.models import Vendor, Product, DocumentationSource

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_source(client):
    """Seed a vendor → product (version=10.0) → source with a versioned base_url."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="VersionedCo")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="SolG", version="10.0")
        s.add(product)
        await s.flush()
        source = DocumentationSource(
            product_id=product.id,
            name="SolG Docs",
            base_url="https://x/UDP/Available/10.0/SolG/default.htm",
        )
        s.add(source)
        await s.commit()
        await s.refresh(source)
        return source


# ── detect-version-token route ──

async def test_detect_version_token_proposes_template(client, seeded_source):
    c, _ = client
    r = await c.post(
        f"/api/sources/{seeded_source.id}/detect-version-token",
        json={"version": "10.0"},
    )
    assert r.status_code == 200
    assert r.json()["url_template"] == "https://x/UDP/Available/{version}/SolG/default.htm"


async def test_detect_version_token_none_when_absent(client, seeded_source):
    c, _ = client
    r = await c.post(
        f"/api/sources/{seeded_source.id}/detect-version-token",
        json={"version": "99.9"},
    )
    assert r.status_code == 200 and r.json()["url_template"] is None


async def test_detect_version_token_unknown_source_404(client):
    c, _ = client
    r = await c.post(
        f"/api/sources/{uuid.uuid4()}/detect-version-token",
        json={"version": "10.0"},
    )
    assert r.status_code == 404


# ── url_template on create / update ──

async def test_create_source_with_url_template_resolves_base_url(client):
    """When url_template is supplied and the product has a version, base_url is
    derived from the template; url_template is persisted as given."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="V")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="P", version="2.1")
        s.add(product)
        await s.commit()
        pid = product.id

    resp = await c.post(
        "/api/sources",
        json={
            "product_id": str(pid),
            "name": "Docs",
            "base_url": "https://docs.example.com/2.1/guide",
            "url_template": "https://docs.example.com/{version}/guide",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["url_template"] == "https://docs.example.com/{version}/guide"
    # base_url resolved from template + product version
    assert body["base_url"] == "https://docs.example.com/2.1/guide"


async def test_create_source_with_url_template_no_product_version(client):
    """When the product has no version, url_template is persisted but base_url
    is left as provided."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="V2")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="P2")  # no version
        s.add(product)
        await s.commit()
        pid = product.id

    resp = await c.post(
        "/api/sources",
        json={
            "product_id": str(pid),
            "name": "Docs",
            "base_url": "https://docs.example.com/latest/guide",
            "url_template": "https://docs.example.com/{version}/guide",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["url_template"] == "https://docs.example.com/{version}/guide"
    assert body["base_url"] == "https://docs.example.com/latest/guide"


async def test_update_source_with_url_template_resolves_base_url(client):
    """PATCH with url_template resolves base_url against the product version."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="V3")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="P3", version="3.0")
        s.add(product)
        await s.flush()
        source = DocumentationSource(
            product_id=product.id,
            name="Docs",
            base_url="https://old.example.com/docs",
        )
        s.add(source)
        await s.commit()
        sid = source.id

    resp = await c.patch(
        f"/api/sources/{sid}",
        json={"url_template": "https://new.example.com/{version}/docs"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["url_template"] == "https://new.example.com/{version}/docs"
    assert body["base_url"] == "https://new.example.com/3.0/docs"


async def test_update_source_combined_product_id_and_url_template_resolves_against_new_product(
    client,
):
    """PATCH with both product_id and url_template must resolve base_url against
    the NEW product's version, not the old one."""
    c, session_factory = client
    async with session_factory() as s:
        vendor = Vendor(name="V_combo")
        s.add(vendor)
        await s.flush()
        product_old = Product(vendor_id=vendor.id, name="P_old", version="10.0")
        product_new = Product(vendor_id=vendor.id, name="P_new", version="11.0")
        s.add(product_old)
        s.add(product_new)
        await s.flush()
        source = DocumentationSource(
            product_id=product_old.id,
            name="Combo Docs",
            base_url="https://docs.example.com/10.0/guide",
        )
        s.add(source)
        await s.commit()
        sid = source.id
        new_pid = product_new.id

    resp = await c.patch(
        f"/api/sources/{sid}",
        json={
            "product_id": str(new_pid),
            "url_template": "https://docs.example.com/{version}/guide",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # url_template stored as provided
    assert body["url_template"] == "https://docs.example.com/{version}/guide"
    # base_url must resolve against the NEW product's version (11.0), not old (10.0)
    assert body["base_url"] == "https://docs.example.com/11.0/guide"
    assert body["product_id"] == str(new_pid)
