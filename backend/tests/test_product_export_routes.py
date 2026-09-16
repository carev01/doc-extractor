"""Product-level (batch) export routes."""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Article, DocumentationSource, Product, Vendor
from app.models.export_job import ExportJob

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(session_factory, with_articles=(3, 2), empty_sources=0):
    """Product with len(with_articles) populated sources + N empty ones."""
    async with session_factory() as s:
        vendor = Vendor(name="Acme")
        s.add(vendor)
        await s.flush()
        product = Product(vendor_id=vendor.id, name="Widget")
        s.add(product)
        await s.flush()
        for i, n in enumerate(with_articles):
            src = DocumentationSource(
                product_id=product.id, name=f"Guide {i}",
                base_url=f"https://d.example.com/{i}",
            )
            s.add(src)
            await s.flush()
            for a in range(n):
                s.add(Article(
                    source_id=src.id, title=f"A{a}", source_url=f"https://d/{i}/{a}",
                    topic_key=f"https://d/{i}/{a}",
                    content_markdown="body " * 10, content_hash=f"h{i}{a}",
                    sort_order=a,
                ))
        for j in range(empty_sources):
            s.add(DocumentationSource(
                product_id=product.id, name=f"Empty {j}",
                base_url=f"https://d.example.com/empty{j}",
            ))
        await s.commit()
        return product.id


async def test_product_export_enqueues_one_job_per_source(client):
    c, sf = client
    pid = await _seed(sf)
    r = await c.post(f"/api/export/product/{pid}", json={"format": "markdown"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["skipped"] == []

    async with sf() as s:
        jobs = (await s.execute(
            select(ExportJob).where(ExportJob.batch_id == uuid.UUID(body["batch_id"]))
            .order_by(ExportJob.batch_seq)
        )).scalars().all()
    assert len(jobs) == 2
    # Each is an ordinary single-source job — the engine is untouched.
    assert all(j.source_id is not None for j in jobs)
    assert [j.batch_seq for j in jobs] == [0, 1]
    assert {j.batch_label for j in jobs} == {"Acme / Widget"}
    assert all(j.request["source_id"] == str(j.source_id) for j in jobs)


async def test_sources_without_articles_are_skipped_not_failed(client):
    """export_sync raises "No articles matched" on an empty source, so queueing
    one would plant a permanent FAILED row the operator can do nothing about."""
    c, sf = client
    pid = await _seed(sf, with_articles=(3,), empty_sources=2)
    r = await c.post(f"/api/export/product/{pid}", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert sorted(body["skipped"]) == ["Empty 0", "Empty 1"]


async def test_a_product_with_nothing_to_export_is_refused(client):
    c, sf = client
    pid = await _seed(sf, with_articles=(), empty_sources=2)
    r = await c.post(f"/api/export/product/{pid}", json={})
    assert r.status_code == 400
    assert "no exportable sources" in r.json()["detail"].lower()


async def test_source_ids_restricts_the_batch(client):
    c, sf = client
    pid = await _seed(sf, with_articles=(3, 2))
    async with sf() as s:
        ids = (await s.execute(
            select(DocumentationSource.id)
            .where(DocumentationSource.product_id == pid)
            .order_by(DocumentationSource.name)
        )).scalars().all()
    r = await c.post(f"/api/export/product/{pid}", json={"source_ids": [str(ids[0])]})
    assert r.status_code == 200 and r.json()["total"] == 1


async def test_preview_projects_size_and_respects_include_images(client):
    c, sf = client
    pid = await _seed(sf, with_articles=(3, 2), empty_sources=1)
    r = await c.get(f"/api/export/product/{pid}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "Acme / Widget"
    assert body["source_count"] == 3 and body["exportable_source_count"] == 2
    assert body["skipped"] == ["Empty 0"]
    assert body["total_articles"] == 5
    assert body["markdown_bytes"] > 0
    # No images seeded, so the projection is the markdown either way.
    assert body["projected_bytes"] == body["markdown_bytes"]
    r2 = await c.get(f"/api/export/product/{pid}/preview?include_images=true")
    assert r2.json()["projected_bytes"] == body["markdown_bytes"]


async def test_batch_status_aggregates_children(client):
    c, sf = client
    pid = await _seed(sf)
    batch_id = (await c.post(f"/api/export/product/{pid}", json={})).json()["batch_id"]
    r = await c.get(f"/api/export/batches/{batch_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["pending"] == 2
    assert body["finished"] is False
    assert body["label"] == "Acme / Widget"
    assert [s["seq"] for s in body["sources"]] == [0, 1]
    assert all(s["status"] == "pending" for s in body["sources"])


async def test_download_refused_while_nothing_has_completed(client):
    c, sf = client
    pid = await _seed(sf)
    batch_id = (await c.post(f"/api/export/product/{pid}", json={})).json()["batch_id"]
    r = await c.get(f"/api/export/batches/{batch_id}/download")
    assert r.status_code == 409


async def test_unknown_batch_is_404(client):
    c, _sf = client
    r = await c.get(f"/api/export/batches/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_include_images_now_defaults_off(client):
    """Flipped deliberately: image payloads dwarf the text, so opting in beats
    discovering the size mid-download."""
    c, sf = client
    pid = await _seed(sf, with_articles=(1,))
    batch_id = (await c.post(f"/api/export/product/{pid}", json={})).json()["batch_id"]
    async with sf() as s:
        job = (await s.execute(
            select(ExportJob).where(ExportJob.batch_id == uuid.UUID(batch_id))
        )).scalars().first()
    assert job.request["include_images"] is False
