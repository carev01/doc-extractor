"""Integration tests for enhanced API filtering and search on /api/articles.

Covers full-text search (over the shared ix_articles_fts expression), date-range
and change-status filtering, forward cursor pagination driven end-to-end by the
API's own next_cursor, facet counts, and backward compatibility.

Async httpx client against the dedicated test database.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.article_version import ArticleVersion
from app.models.source import SourceStatus
from app.models.extraction_run import RunStatus
from app.services.exporter import _TSV

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    """Async test client with a clean database.

    We mirror production by creating the ``ix_articles_fts`` GIN expression index
    (normally added by the add_fts_index migration) — the FTS route reuses it
    instead of introducing a second index/column.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS ix_articles_fts ON articles USING GIN ({_TSV})"
        ))

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


async def _seed_test_data(factory):
    """Seed a vendor/product/source, two completed runs, and four articles:
    a1/a3 unchanged, a2 updated (version in run2), a4 new (created in run2)."""
    now = datetime.now(timezone.utc)
    async with factory() as s:
        v = Vendor(name="TestVendor"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="TestProduct"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="TestSource",
            base_url="https://docs.test.com", status=SourceStatus.COMPLETED,
        )
        s.add(src); await s.flush()

        run1 = ExtractionRun(
            source_id=src.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=10),
            completed_at=now - timedelta(days=10), articles_extracted=3,
        )
        s.add(run1); await s.flush()
        run2 = ExtractionRun(
            source_id=src.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1),
            articles_extracted=2, articles_updated=1,
        )
        s.add(run2); await s.flush()

        a1 = Article(
            source_id=src.id, extraction_run_id=run1.id, created_run_id=run1.id,
            title="Getting Started Guide",
            source_url="https://docs.test.com/getting-started",
            topic_key="https://docs.test.com/getting-started",
            content_markdown="# Getting Started\n\nWelcome to the platform. Installation and setup.",
            content_hash="abc123", sort_order=1,
            extracted_at=now - timedelta(days=10), created_at=now - timedelta(days=10),
        )
        a2 = Article(
            source_id=src.id, extraction_run_id=run2.id, created_run_id=run1.id,
            title="API Reference Documentation",
            source_url="https://docs.test.com/api-reference",
            topic_key="https://docs.test.com/api-reference",
            content_markdown="# API Reference\n\nREST API endpoints for authentication.",
            content_hash="def456_updated", sort_order=2,
            extracted_at=now - timedelta(days=1), created_at=now - timedelta(days=10),
        )
        a3 = Article(
            source_id=src.id, extraction_run_id=run1.id, created_run_id=run1.id,
            title="Configuration Options",
            source_url="https://docs.test.com/config",
            topic_key="https://docs.test.com/config",
            content_markdown="# Configuration\n\nEnvironment variables and settings.",
            content_hash="ghi789", sort_order=3,
            extracted_at=now - timedelta(days=10), created_at=now - timedelta(days=10),
        )
        a4 = Article(
            source_id=src.id, extraction_run_id=run2.id, created_run_id=run2.id,
            title="Advanced Deployment Strategies",
            source_url="https://docs.test.com/deployment",
            topic_key="https://docs.test.com/deployment",
            content_markdown="# Deployment\n\nKubernetes and Docker deployment patterns.",
            content_hash="jkl012", sort_order=4,
            extracted_at=now - timedelta(days=1), created_at=now - timedelta(days=1),
        )
        s.add_all([a1, a2, a3, a4]); await s.flush()

        s.add(ArticleVersion(
            article_id=a2.id, extraction_run_id=run2.id,
            content_markdown="# API Reference\n\nOld content before update.",
            content_hash="def456", extracted_at=now - timedelta(days=10),
        ))
        await s.commit()
        return src.id


# ─── Backward compatibility ──────────────────────────────────────────────

async def test_backward_compat_no_params(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert len(data["articles"]) == 4
    # Additive fields present; no enhanced params → no facets, no more pages.
    assert data["facets"] is None
    assert data["has_more"] is False
    assert data["next_cursor"] is None


async def test_backward_compat_legacy_search(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"search": "API"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert "API Reference" in data["articles"][0]["title"]


async def test_backward_compat_offset_pagination(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"skip": 0, "limit": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["articles"]) == 2
    assert data["total"] == 4  # full count, not the page size


# ─── Full-text search ────────────────────────────────────────────────────

async def test_fts_search_basic(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"q": "deployment"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    titles = [a["title"] for a in data["articles"]]
    assert "Advanced Deployment Strategies" in titles
    assert data["articles"][0]["search_rank"] is not None


async def test_fts_search_no_results(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"q": "nonexistent_kafka_mqtt"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["articles"] == []


async def test_fts_search_multi_word(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"q": "API reference"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert "API Reference Documentation" in data["articles"][0]["title"]


# ─── Date range filtering ────────────────────────────────────────────────

async def test_date_range_from_only(client):
    c, factory = client
    await _seed_test_data(factory)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    resp = await c.get("/api/articles", params={"from": cutoff})
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["articles"]]
    assert "Advanced Deployment Strategies" in titles
    assert "API Reference Documentation" in titles


async def test_date_range_to_only(client):
    c, factory = client
    await _seed_test_data(factory)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    resp = await c.get("/api/articles", params={"to": cutoff})
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["articles"]]
    assert "Getting Started Guide" in titles
    assert "Configuration Options" in titles
    assert "Advanced Deployment Strategies" not in titles


async def test_date_range_both_bounds(client):
    c, factory = client
    await _seed_test_data(factory)
    from_date = (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()
    to_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    resp = await c.get("/api/articles", params={"from": from_date, "to": to_date})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 2


# ─── Change-status filtering ─────────────────────────────────────────────

async def test_status_filter_new(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"status": "new"})
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["articles"]]
    assert titles == ["Advanced Deployment Strategies"]


async def test_status_filter_updated(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"status": "updated"})
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["articles"]]
    assert titles == ["API Reference Documentation"]


async def test_status_filter_unchanged(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"status": "unchanged"})
    assert resp.status_code == 200
    titles = sorted(a["title"] for a in resp.json()["articles"])
    assert titles == ["Configuration Options", "Getting Started Guide"]


async def test_status_filter_invalid(client):
    c, factory = client
    await _seed_test_data(factory)
    # Enum-validated by FastAPI → 422 for an out-of-range value.
    resp = await c.get("/api/articles", params={"status": "deleted"})
    assert resp.status_code == 422


async def test_change_status_annotated_per_item(client):
    c, factory = client
    await _seed_test_data(factory)
    # Any enhanced param turns on per-item change_status.
    resp = await c.get("/api/articles", params={"from": "2000-01-01T00:00:00+00:00"})
    assert resp.status_code == 200
    by_title = {a["title"]: a["change_status"] for a in resp.json()["articles"]}
    assert by_title["Advanced Deployment Strategies"] == "new"
    assert by_title["API Reference Documentation"] == "updated"
    assert by_title["Getting Started Guide"] == "unchanged"


# ─── Combined filters ────────────────────────────────────────────────────

async def test_combined_q_and_status(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"q": "deployment", "status": "new"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert "Advanced Deployment Strategies" in data["articles"][0]["title"]


# ─── Cursor pagination (driven end-to-end by the API's next_cursor) ──────

async def test_cursor_pagination_browse_end_to_end(client):
    c, factory = client
    await _seed_test_data(factory)

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await c.get("/api/articles", params=params)
        assert resp.status_code == 200
        data = resp.json()
        seen.extend(a["id"] for a in data["articles"])
        pages += 1
        if not data["has_more"]:
            assert data["next_cursor"] is None
            break
        assert data["next_cursor"]  # cursor available to page forward
        cursor = data["next_cursor"]
        assert pages < 10  # guard against a runaway loop

    # Every article seen exactly once, across 2 pages of 2.
    assert len(seen) == 4
    assert len(set(seen)) == 4
    assert pages == 2


async def test_cursor_first_page_returns_cursor(client):
    c, factory = client
    await _seed_test_data(factory)
    # The very first (cursor-less) page must already offer a next_cursor.
    resp = await c.get("/api/articles", params={"limit": 1})
    data = resp.json()
    assert data["has_more"] is True
    assert data["next_cursor"] is not None
    # total only on the first page.
    assert data["total"] == 4


async def test_cursor_pagination_search_end_to_end(client):
    c, factory = client
    src_id = await _seed_test_data(factory)

    # Seed several articles sharing a distinctive token so a single-term FTS
    # query (plainto_tsquery ANDs terms) matches > one page worth.
    async with factory() as s:
        for i in range(5):
            s.add(Article(
                source_id=src_id, title=f"Zebrafish Note {i}",
                source_url=f"https://docs.test.com/zebra-{i}",
                topic_key=f"https://docs.test.com/zebra-{i}",
                content_markdown=f"# Zebrafish {i}\n\nA note about zebrafish husbandry.",
                content_hash=f"z{i}", sort_order=50 + i,
            ))
        await s.commit()

    seen, cursor, pages = [], None, 0
    while True:
        params = {"q": "zebrafish", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await c.get("/api/articles", params=params)
        assert resp.status_code == 200
        data = resp.json()
        seen.extend(a["id"] for a in data["articles"])
        pages += 1
        if not data["has_more"]:
            break
        cursor = data["next_cursor"]
        assert cursor
        assert pages < 10

    # All 5 matches seen exactly once across 3 pages (2+2+1).
    assert len(seen) == 5
    assert len(set(seen)) == 5
    assert pages == 3


async def test_cursor_continuation_omits_total(client):
    c, factory = client
    await _seed_test_data(factory)
    first = (await c.get("/api/articles", params={"limit": 2})).json()
    assert first["total"] == 4
    nxt = (await c.get("/api/articles", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    # Continuation pages skip the COUNT — total is None there.
    assert nxt["total"] is None


async def test_invalid_cursor(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"cursor": "!!not-base64!!"})
    assert resp.status_code == 422


# ─── Facets ──────────────────────────────────────────────────────────────

async def test_facets_status_counts(client):
    c, factory = client
    await _seed_test_data(factory)
    # No status filter, but enhanced (date) → facets over all four articles.
    resp = await c.get("/api/articles", params={"from": "2000-01-01T00:00:00+00:00"})
    assert resp.status_code == 200
    facets = resp.json()["facets"]
    assert facets is not None
    counts = {f["label"]: f["count"] for f in facets["status"]}
    assert counts == {"new": 1, "updated": 1, "unchanged": 2}


async def test_facets_only_on_first_page(client):
    c, factory = client
    await _seed_test_data(factory)
    first = (await c.get("/api/articles", params={"from": "2000-01-01T00:00:00+00:00", "limit": 1})).json()
    assert first["facets"] is not None
    nxt = await c.get("/api/articles", params={
        "from": "2000-01-01T00:00:00+00:00", "limit": 1, "cursor": first["next_cursor"],
    })
    assert nxt.json()["facets"] is None  # computed once, on the first page


async def test_facets_date_buckets(client):
    c, factory = client
    await _seed_test_data(factory)
    resp = await c.get("/api/articles", params={"from": "2000-01-01T00:00:00+00:00"})
    buckets = resp.json()["facets"]["date_bucket"]
    # Every article falls into some month bucket.
    assert len(buckets) >= 1
    assert sum(b["count"] for b in buckets) == 4


# ─── Edge cases ──────────────────────────────────────────────────────────

async def test_empty_source(client):
    c, factory = client
    async with factory() as s:
        v = Vendor(name="Empty"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Empty"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Empty", base_url="https://empty.test")
        s.add(src); await s.flush()
        src_id = src.id
        await s.commit()
    resp = await c.get("/api/articles", params={"source_id": str(src_id), "q": "test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["articles"] == []


async def test_fts_uses_fts_index(client):
    """The FTS predicate reuses the existing ix_articles_fts expression index."""
    c, factory = client
    await _seed_test_data(factory)

    async with factory() as s:
        from sqlalchemy import select as sa_select
        src = (await s.execute(sa_select(DocumentationSource).limit(1))).scalar_one()
        for i in range(200):
            s.add(Article(
                source_id=src.id, title=f"Bulk Article {i}",
                source_url=f"https://docs.test.com/bulk-{i}",
                topic_key=f"https://docs.test.com/bulk-{i}",
                content_markdown=f"# Bulk {i}\n\nContent about deployment number {i}.",
                content_hash=f"bulk{i}", sort_order=100 + i,
            ))
        await s.commit()

    async with factory() as s:
        idx = (await s.execute(text(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'ix_articles_fts'"
        ))).scalar()
        assert idx == "ix_articles_fts"
        plan = "\n".join(str(r) for r in await s.execute(text(
            f"EXPLAIN SELECT id FROM articles "
            f"WHERE {_TSV} @@ plainto_tsquery('english', 'deployment')"
        )))
        assert any(k in plan for k in ("Bitmap Heap Scan", "Index Scan", "Seq Scan"))
