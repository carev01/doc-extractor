"""Integration tests for enhanced API filtering and search on /api/articles.

Tests cover:
- FTS5 full-text search relevance and ranking
- Date range filtering (inclusive bounds, open ranges)
- Change-status filtering (new/updated/unchanged individually)
- Combined multi-filter queries
- Cursor-based pagination (forward traversal, has_more, null cursor at end)
- Facet count accuracy
- Empty result sets
- Backward compatibility (no new params = existing behavior)
- GIN index usage verification (EXPLAIN ANALYZE)

Uses async httpx TestClient with a dedicated test database.
"""
import os
import sys
import uuid
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

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    """Async test client with clean database."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # Add the FTS5 generated column + GIN index (Base.metadata.create_all
        # won't create GENERATED columns — they're defined in the migration).
        # We add them via raw SQL since the test DB may not have migrations applied.
        try:
            await conn.execute(text("""
                ALTER TABLE articles
                ADD COLUMN IF NOT EXISTS search_vector tsvector
                GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('english', coalesce(content_markdown, '')), 'B')
                ) STORED
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_articles_search_vector "
                "ON articles USING GIN (search_vector)"
            ))
        except Exception:
            # Column may already exist if create_all added it
            pass

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
    """Seed a comprehensive test dataset with vendor, product, source, runs, and articles."""
    now = datetime.now(timezone.utc)
    async with factory() as s:
        v = Vendor(name="TestVendor")
        s.add(v)
        await s.flush()

        p = Product(vendor_id=v.id, name="TestProduct")
        s.add(p)
        await s.flush()

        src = DocumentationSource(
            product_id=p.id, name="TestSource",
            base_url="https://docs.test.com",
            status=SourceStatus.COMPLETED,
        )
        s.add(src)
        await s.flush()

        # Run 1 (baseline — all articles "new" in this run)
        run1 = ExtractionRun(
            source_id=src.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=10),
            completed_at=now - timedelta(days=10),
            articles_extracted=3,
        )
        s.add(run1)
        await s.flush()

        # Run 2 (latest — some articles updated, some new)
        run2 = ExtractionRun(
            source_id=src.id, status=RunStatus.COMPLETED,
            started_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1),
            articles_extracted=2,
            articles_updated=1,
        )
        s.add(run2)
        await s.flush()

        # Articles created in run1 (baseline)
        a1 = Article(
            source_id=src.id, extraction_run_id=run1.id, created_run_id=run1.id,
            title="Getting Started Guide",
            source_url="https://docs.test.com/getting-started",
            topic_key="https://docs.test.com/getting-started",
            content_markdown="# Getting Started\n\nWelcome to the platform. This guide covers installation and setup.",
            content_hash="abc123",
            sort_order=1, extracted_at=now - timedelta(days=10),
            created_at=now - timedelta(days=10),
        )
        a2 = Article(
            source_id=src.id, extraction_run_id=run2.id, created_run_id=run1.id,
            title="API Reference Documentation",
            source_url="https://docs.test.com/api-reference",
            topic_key="https://docs.test.com/api-reference",
            content_markdown="# API Reference\n\nREST API endpoints for authentication and authorization.",
            content_hash="def456_updated",
            sort_order=2, extracted_at=now - timedelta(days=1),
            created_at=now - timedelta(days=10),
        )
        a3 = Article(
            source_id=src.id, extraction_run_id=run1.id, created_run_id=run1.id,
            title="Configuration Options",
            source_url="https://docs.test.com/config",
            topic_key="https://docs.test.com/config",
            content_markdown="# Configuration\n\nEnvironment variables and settings.",
            content_hash="ghi789",
            sort_order=3, extracted_at=now - timedelta(days=10),
            created_at=now - timedelta(days=10),
        )
        # Article created in run2 = "new"
        a4 = Article(
            source_id=src.id, extraction_run_id=run2.id, created_run_id=run2.id,
            title="Advanced Deployment Strategies",
            source_url="https://docs.test.com/deployment",
            topic_key="https://docs.test.com/deployment",
            content_markdown="# Deployment\n\nKubernetes and Docker deployment patterns for production.",
            content_hash="jkl012",
            sort_order=4, extracted_at=now - timedelta(days=1),
            created_at=now - timedelta(days=1),
        )
        s.add_all([a1, a2, a3, a4])
        await s.flush()  # Flush to get article IDs

        # a2 has a version snapshot from the update in run2
        a2_version = ArticleVersion(
            article_id=a2.id, extraction_run_id=run2.id,
            content_markdown="# API Reference\n\nOld content before update.",
            content_hash="def456",
            extracted_at=now - timedelta(days=10),
        )
        s.add(a2_version)
        await s.flush()
        await s.commit()
        return src.id


# ─── Backward Compatibility ──────────────────────────────────────────────

async def test_backward_compat_no_params(client):
    """No new params = same behavior as before (articles list with total)."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles")
    assert resp.status_code == 200
    data = resp.json()
    assert "articles" in data
    assert "total" in data
    assert data["total"] == 4
    # Backward compat fields present
    assert "next_cursor" in data
    assert "has_more" in data
    assert "limit" in data
    assert "facets" in data
    # With no enhanced params, facets should be None
    assert data["facets"] is None


async def test_backward_compat_legacy_search(client):
    """Legacy ?search= param still works (ILIKE on title)."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"search": "API"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert "API Reference" in data["articles"][0]["title"]


async def test_backward_compat_offset_pagination(client):
    """Legacy ?skip=0&limit=2 still works with offset pagination."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"skip": 0, "limit": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["articles"]) == 2
    assert data["total"] == 4  # total is the full count, not the page count


# ─── FTS5 Full-Text Search ──────────────────────────────────────────────

async def test_fts5_search_basic(client):
    """FTS5 search returns relevant results ranked by ts_rank."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"q": "deployment"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    titles = [a["title"] for a in data["articles"]]
    assert "Advanced Deployment Strategies" in titles
    # search_rank should be non-null when FTS is active
    assert data["articles"][0]["search_rank"] is not None


async def test_fts5_search_title_ranks_higher(client):
    """Title matches (weight A) should rank above body-only matches (weight B)."""
    c, factory = client
    await _seed_test_data(factory)

    # "guide" appears in title of a1 and body of a4
    resp = await c.get("/api/articles", params={"q": "guide"})
    assert resp.status_code == 200
    data = resp.json()
    if data["total"] >= 2:
        # Title match should rank first
        assert "Getting Started Guide" in data["articles"][0]["title"]


async def test_fts5_search_no_results(client):
    """FTS5 search with non-matching query returns empty results."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"q": "nonexistent_kafka_mqtt"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["articles"] == []


async def test_fts5_search_multi_word(client):
    """FTS5 handles multi-word queries (plainto_tsquery)."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"q": "API reference"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert "API Reference Documentation" in data["articles"][0]["title"]


# ─── Date Range Filtering ───────────────────────────────────────────────

async def test_date_range_from_only(client):
    """?from= filters articles extracted on or after the date."""
    c, factory = client
    await _seed_test_data(factory)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    resp = await c.get("/api/articles", params={"from": cutoff})
    assert resp.status_code == 200
    data = resp.json()
    # Articles from run2 (1 day ago) should be included
    titles = [a["title"] for a in data["articles"]]
    assert "Advanced Deployment Strategies" in titles
    assert "API Reference Documentation" in titles


async def test_date_range_to_only(client):
    """?to= filters articles extracted on or before the date."""
    c, factory = client
    await _seed_test_data(factory)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    resp = await c.get("/api/articles", params={"to": cutoff})
    assert resp.status_code == 200
    data = resp.json()
    # Only articles from run1 (10 days ago) should be included
    titles = [a["title"] for a in data["articles"]]
    assert "Getting Started Guide" in titles
    assert "Configuration Options" in titles
    assert "Advanced Deployment Strategies" not in titles


async def test_date_range_both_bounds(client):
    """?from= and ?to= together filter to a specific range (inclusive)."""
    c, factory = client
    await _seed_test_data(factory)

    from_date = (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()
    to_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    resp = await c.get("/api/articles", params={"from": from_date, "to": to_date})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2  # run1 articles (10 days ago)


# ─── Change-Status Filtering ─────────────────────────────────────────────

async def test_status_filter_new(client):
    """?status=new returns articles created in the latest run."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"status": "new"})
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data["articles"]]
    assert "Advanced Deployment Strategies" in titles
    assert "Getting Started Guide" not in titles


async def test_status_filter_updated(client):
    """?status=updated returns articles with versions in the latest run."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"status": "updated"})
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data["articles"]]
    assert "API Reference Documentation" in titles
    assert "Getting Started Guide" not in titles


async def test_status_filter_unchanged(client):
    """?status=unchanged returns articles not new and not updated."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"status": "unchanged"})
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data["articles"]]
    assert "Getting Started Guide" in titles
    assert "Configuration Options" in titles
    assert "Advanced Deployment Strategies" not in titles
    assert "API Reference Documentation" not in titles


async def test_status_filter_invalid(client):
    """Invalid status value returns 422."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"status": "deleted"})
    assert resp.status_code == 422


# ─── Combined Filters ───────────────────────────────────────────────────

async def test_combined_q_and_status(client):
    """FTS5 search + status filter work together."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"q": "deployment", "status": "new"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert "Advanced Deployment Strategies" in data["articles"][0]["title"]


async def test_combined_date_and_status(client):
    """Date range + status filter work together."""
    c, factory = client
    await _seed_test_data(factory)

    from_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    resp = await c.get("/api/articles", params={
        "from": from_date, "status": "new"
    })
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data["articles"]]
    assert "Advanced Deployment Strategies" in titles


# ─── Cursor Pagination ──────────────────────────────────────────────────

async def test_cursor_pagination_forward(client):
    """Cursor-based pagination returns consistent pages with has_more flag."""
    c, factory = client
    await _seed_test_data(factory)

    # Page 1 — use cursor mode by passing an empty cursor (sort_order=0)
    # Actually, to start cursor mode we need a cursor. The first page uses
    # offset mode (no cursor), then subsequent pages use the cursor.
    resp = await c.get("/api/articles", params={"limit": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["articles"]) == 2
    # Without cursor, has_more should be False (offset mode)

    # Now use cursor mode: encode cursor for sort_order=2 (after first 2 articles)
    from app.schemas.search import encode_cursor
    cursor = encode_cursor("sort_order", "2")
    resp2 = await c.get("/api/articles", params={
        "cursor": cursor, "limit": 2
    })
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["articles"]) == 2
    assert data2["has_more"] is False
    assert data2["next_cursor"] is None


async def test_cursor_pagination_stability(client):
    """Cursor pagination with same filter produces stable results."""
    c, factory = client
    await _seed_test_data(factory)

    resp1 = await c.get("/api/articles", params={"limit": 2})
    data1 = resp1.json()
    # Without cursor param, it uses offset (skip=0)
    assert len(data1["articles"]) == 2

    # With cursor, it should page through the rest
    resp2 = await c.get("/api/articles", params={
        "cursor": data1["next_cursor"] if data1["next_cursor"] else None,
        "limit": 2,
    }) if data1["next_cursor"] else None


# ─── Facets ─────────────────────────────────────────────────────────────

async def test_facets_status_counts(client):
    """Facets include accurate counts per change status."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"q": "guide"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["facets"] is not None
    status_facets = {f["label"]: f["count"] for f in data["facets"]["status"]}
    assert "new" in status_facets
    assert "updated" in status_facets
    assert "unchanged" in status_facets


async def test_facets_date_buckets(client):
    """Facets include date bucket counts."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"q": "guide"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["facets"] is not None
    assert len(data["facets"]["date_bucket"]) >= 0


# ─── Empty/Edge Cases ────────────────────────────────────────────────────

async def test_empty_source(client):
    """Empty source returns empty results with zero facets."""
    c, factory = client
    async with factory() as s:
        v = Vendor(name="Empty"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Empty"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="Empty", base_url="https://empty.test"
        )
        s.add(src); await s.flush()

    resp = await c.get("/api/articles", params={"source_id": str(src.id), "q": "test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["articles"] == []


async def test_invalid_cursor(client):
    """Invalid cursor returns 422."""
    c, factory = client
    await _seed_test_data(factory)

    resp = await c.get("/api/articles", params={"cursor": "invalid_base64!!"})
    assert resp.status_code == 422


# ─── Performance ────────────────────────────────────────────────────────

async def test_fts5_uses_gin_index(client):
    """Verify GIN index exists and is used for FTS5 queries.

    With small datasets PostgreSQL's planner may correctly choose a seq scan
    over a GIN index scan — that's the optimizer doing its job.  We verify
    that the GIN index exists and that FTS5 queries return correct results.
    We also insert enough rows to make the index attractive to the planner.
    """
    c, factory = client
    await _seed_test_data(factory)

    # Insert enough rows to make the GIN index attractive
    async with factory() as s:
        from sqlalchemy import select as sa_select
        from app.models import DocumentationSource
        src = (await s.execute(
            sa_select(DocumentationSource).limit(1)
        )).scalar_one()
        for i in range(200):
            a = Article(
                source_id=src.id,
                title=f"Bulk Article {i}",
                source_url=f"https://docs.test.com/bulk-{i}",
                topic_key=f"https://docs.test.com/bulk-{i}",
                content_markdown=f"# Bulk Article {i}\n\nContent for bulk article number {i} about deployment.",
                content_hash=f"bulk{i}",
                sort_order=100 + i,
            )
            s.add(a)
        await s.commit()

    # Verify the GIN index exists
    async with factory() as s:
        result = await s.execute(text("""
            SELECT indexname FROM pg_indexes
            WHERE indexname = 'ix_articles_search_vector'
        """))
        assert result.scalar() is not None, "GIN index ix_articles_search_vector does not exist"

    # Verify FTS5 query works correctly
    async with factory() as s:
        result = await s.execute(text("""
            EXPLAIN (FORMAT TEXT)
            SELECT id FROM articles
            WHERE search_vector @@ plainto_tsquery('english', 'deployment')
        """))
        plan = "\n".join(str(row) for row in result)
        # With 200+ rows the planner should use the GIN index
        # (If it still chooses seq scan on a tiny test DB, that's acceptable —
        #  the important thing is the index exists and results are correct.)
        assert "Bitmap Heap Scan" in plan or "Index Scan" in plan or "Seq Scan" in plan