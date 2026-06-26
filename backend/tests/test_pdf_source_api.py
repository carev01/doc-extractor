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


async def test_oversize_upload_is_413(client):
    c, factory = client
    settings.pdf_max_upload_bytes = 10
    pid = await _product(factory)
    files = {"file": ("d.pdf", io.BytesIO(b"%PDF-1.4 " + b"x" * 100), "application/pdf")}
    resp = await c.post("/api/sources/pdf",
                        data={"product_id": str(pid), "name": "Big"}, files=files)
    assert resp.status_code == 413
    settings.pdf_max_upload_bytes = 100 * 1024 * 1024
