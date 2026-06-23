"""Tests for the Product layer (Vendor -> Product -> Source).

Exercises the async product routes end-to-end via httpx.AsyncClient with get_db
overridden to point at docextractor_test, plus the source nesting/move behaviour
and the vendor->product->source cascade. Mirrors the harness in test_versions.py.
"""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


async def _vendor(TestSession, name="Acme Corp") -> uuid.UUID:
    async with TestSession() as s:
        v = Vendor(name=name)
        s.add(v)
        await s.commit()
        return v.id


# ── Product CRUD routes ──

async def test_create_product_under_vendor(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    resp = await c.post("/api/products", json={"vendor_id": str(vid), "name": "Cloud"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Cloud"
    assert body["vendor_id"] == str(vid)


async def test_create_product_unknown_vendor_404(client):
    c, _ = client
    resp = await c.post(
        "/api/products", json={"vendor_id": str(uuid.uuid4()), "name": "X"}
    )
    assert resp.status_code == 404


async def test_list_products_filtered_by_vendor(client):
    c, TestSession = client
    v1 = await _vendor(TestSession, "V1")
    v2 = await _vendor(TestSession, "V2")
    await c.post("/api/products", json={"vendor_id": str(v1), "name": "P1a"})
    await c.post("/api/products", json={"vendor_id": str(v1), "name": "P1b"})
    await c.post("/api/products", json={"vendor_id": str(v2), "name": "P2"})

    resp = await c.get("/api/products", params={"vendor_id": str(v1)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {p["name"] for p in body["products"]} == {"P1a", "P1b"}


async def test_rename_product(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    pid = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "Old"})).json()["id"]
    resp = await c.patch(f"/api/products/{pid}", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"


# ── Source nesting under products ──

async def test_create_source_under_product(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    pid = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P"})).json()["id"]
    resp = await c.post(
        "/api/sources",
        json={"product_id": pid, "name": "Docs", "base_url": "https://d.acme.com"},
    )
    assert resp.status_code == 201
    assert resp.json()["product_id"] == pid


async def test_create_source_unknown_product_404(client):
    c, _ = client
    resp = await c.post(
        "/api/sources",
        json={"product_id": str(uuid.uuid4()), "name": "S", "base_url": "http://x"},
    )
    assert resp.status_code == 404


async def test_list_sources_by_product_and_by_vendor(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    p1 = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P1"})).json()["id"]
    p2 = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P2"})).json()["id"]
    await c.post("/api/sources", json={"product_id": p1, "name": "S1", "base_url": "http://1"})
    await c.post("/api/sources", json={"product_id": p2, "name": "S2", "base_url": "http://2"})

    # Direct product filter.
    by_p1 = (await c.get("/api/sources", params={"product_id": p1})).json()
    assert by_p1["total"] == 1 and by_p1["sources"][0]["name"] == "S1"

    # Vendor filter resolves through products (both sources).
    by_vendor = (await c.get("/api/sources", params={"vendor_id": str(vid)})).json()
    assert by_vendor["total"] == 2
    assert {s["name"] for s in by_vendor["sources"]} == {"S1", "S2"}


async def test_move_source_to_another_product(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    p1 = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P1"})).json()["id"]
    p2 = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P2"})).json()["id"]
    sid = (await c.post("/api/sources", json={"product_id": p1, "name": "S", "base_url": "http://x"})).json()["id"]

    resp = await c.patch(f"/api/sources/{sid}", json={"product_id": p2})
    assert resp.status_code == 200
    assert resp.json()["product_id"] == p2

    # Moving to a non-existent product is rejected.
    bad = await c.patch(f"/api/sources/{sid}", json={"product_id": str(uuid.uuid4())})
    assert bad.status_code == 404


# ── Cascade: deleting a product / vendor removes its sources ──

async def test_delete_product_cascades_to_sources(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    pid = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P"})).json()["id"]
    await c.post("/api/sources", json={"product_id": pid, "name": "S", "base_url": "http://x"})

    assert (await c.delete(f"/api/products/{pid}")).status_code == 204

    async with TestSession() as s:
        remaining = (await s.execute(select(DocumentationSource))).scalars().all()
        assert remaining == []


async def test_delete_vendor_cascades_to_products_and_sources(client):
    c, TestSession = client
    vid = await _vendor(TestSession)
    pid = (await c.post("/api/products", json={"vendor_id": str(vid), "name": "P"})).json()["id"]
    await c.post("/api/sources", json={"product_id": pid, "name": "S", "base_url": "http://x"})

    assert (await c.delete(f"/api/vendors/{vid}")).status_code == 204

    async with TestSession() as s:
        assert (await s.execute(select(Product))).scalars().all() == []
        assert (await s.execute(select(DocumentationSource))).scalars().all() == []
