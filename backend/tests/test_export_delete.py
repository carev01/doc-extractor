"""Tests for the manual export-delete endpoint (DELETE /api/export/{export_id})."""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, ExportJob
from app.models.export_job import ExportStatus
from app.services.exporter import export_engine

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(export_engine, "export_dir", str(tmp_path))
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
        yield c, factory, str(tmp_path)
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _completed_export(factory, export_dir, with_dir=True) -> uuid.UUID:
    eid = uuid.uuid4()
    if with_dir:
        sub = os.path.join(export_dir, str(eid))
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "out.pdf"), "wb") as f:
            f.write(b"x" * 1024)
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Docs", base_url="https://d")
        s.add(src); await s.flush()
        s.add(ExportJob(
            source_id=src.id, request={"format": "pdf"}, status=ExportStatus.COMPLETED,
            export_id=eid, result={"total_size_bytes": 1024, "file_count": 1, "files": []},
        ))
        await s.commit()
    return eid


async def test_delete_removes_dir_and_row(client):
    c, factory, export_dir = client
    eid = await _completed_export(factory, export_dir)
    assert os.path.isdir(os.path.join(export_dir, str(eid)))

    resp = await c.delete(f"/api/export/{eid}")
    assert resp.status_code == 204
    assert not os.path.exists(os.path.join(export_dir, str(eid)))
    async with factory() as s:
        rows = (await s.execute(select(ExportJob).where(ExportJob.export_id == eid))).scalars().all()
        assert rows == []
    # and it disappears from the listing
    listed = (await c.get("/api/export/list")).json()["exports"]
    assert all(e["export_id"] != str(eid) for e in listed)


async def test_delete_unknown_is_404(client):
    c, _, _ = client
    assert (await c.delete(f"/api/export/{uuid.uuid4()}")).status_code == 404


async def test_delete_row_without_dir_still_succeeds(client):
    c, factory, export_dir = client
    eid = await _completed_export(factory, export_dir, with_dir=False)
    resp = await c.delete(f"/api/export/{eid}")
    assert resp.status_code == 204
    async with factory() as s:
        rows = (await s.execute(select(ExportJob).where(ExportJob.export_id == eid))).scalars().all()
        assert rows == []
