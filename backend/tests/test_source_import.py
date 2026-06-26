"""POST /api/sources/import — CSV bulk import with auto-created vendors/products."""
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
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
async def client():
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


CSV = (
    "vendor,product,source_name,base_url,url_template\n"
    "Acme,Cloud,Guide,https://acme/guide,\n"
    "Acme,Cloud,API,https://acme/api,https://acme/api/{version}\n"
    "Beta,Box,Docs,https://beta/docs,\n"
)


async def test_import_creates_vendors_products_sources(client):
    c, factory = client
    res = (await c.post("/api/sources/import", json={"csv": CSV})).json()
    assert res["created"] == 3 and res["skipped"] == 0 and res["errors"] == 0

    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(Vendor))).scalar() == 2
        assert (await s.execute(select(func.count()).select_from(Product))).scalar() == 2
        assert (await s.execute(select(func.count()).select_from(DocumentationSource))).scalar() == 3


async def test_import_reuses_existing_and_skips_duplicate(client):
    c, factory = client
    await c.post("/api/sources/import", json={"csv": CSV})
    # Re-import the same CSV: same (product, base_url) → all skipped, no new vendors.
    res = (await c.post("/api/sources/import", json={"csv": CSV})).json()
    assert res["created"] == 0 and res["skipped"] == 3

    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(Vendor))).scalar() == 2


async def test_import_bad_row_recorded_without_aborting(client):
    c, _ = client
    csv = (
        "vendor,product,source_name,base_url\n"
        "Acme,Cloud,Good,https://acme/good\n"
        "Acme,Cloud,,https://acme/missing-name\n"   # missing source_name
    )
    res = (await c.post("/api/sources/import", json={"csv": csv})).json()
    assert res["created"] == 1 and res["errors"] == 1
    bad = next(r for r in res["rows"] if r["result"] == "error")
    assert "source_name" in bad["message"]


async def test_import_malformed_csv_is_422(client):
    c, _ = client
    res = await c.post("/api/sources/import", json={"csv": "not a real,csv\nonly one row"})
    assert res.status_code == 422


async def test_import_intra_request_duplicate_skipped(client):
    """A CSV with the same vendor+product+base_url on two rows yields created=1, skipped=1."""
    c, _ = client
    csv = (
        "vendor,product,source_name,base_url,url_template\n"
        "DupVendor,DupProd,GuideV1,https://dup/guide,\n"
        "DupVendor,DupProd,GuideV1,https://dup/guide,\n"  # exact duplicate
    )
    res = (await c.post("/api/sources/import", json={"csv": csv})).json()
    assert res["created"] == 1 and res["skipped"] == 1 and res["errors"] == 0
