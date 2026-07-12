"""GET /api/dashboard/overview — consolidated per-source dashboard + aggregates."""
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
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.models.extraction_run import RunStatus
from app.models.image import ArticleImage

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ctx():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_overview(factory):
    """A: web, completed, 2 articles, latest run (3 new/1 updated/200 unchanged),
    images = 2 described + 3 pending + 1 decorative.
    B: pdf, completed, latest run with escalation_pending = 3 segments, no images.
    C: web, never extracted, no runs.
    """
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()

        from app.models.source import SourceStatus

        src_a = DocumentationSource(
            name="A", base_url="https://a", product_id=p.id,
            source_type="web", status=SourceStatus.COMPLETED,
        )
        src_b = DocumentationSource(
            name="B", base_url="https://b", product_id=p.id,
            source_type="pdf", status=SourceStatus.COMPLETED,
        )
        src_c = DocumentationSource(
            name="C", base_url="https://c", product_id=p.id,
            source_type="web", status=SourceStatus.PENDING,
        )
        s.add_all([src_a, src_b, src_c]); await s.flush()

        from datetime import datetime, timezone
        src_a.last_extracted_at = datetime.now(timezone.utc)
        src_b.last_extracted_at = datetime.now(timezone.utc)
        src_c.last_extracted_at = None

        art_a1 = Article(
            source_id=src_a.id, title="A1", source_url="https://a/1", topic_key="https://a/1",
            content_markdown="# A1", content_hash="h-a1",
        )
        art_a2 = Article(
            source_id=src_a.id, title="A2", source_url="https://a/2", topic_key="https://a/2",
            content_markdown="# A2", content_hash="h-a2",
        )
        s.add_all([art_a1, art_a2]); await s.flush()

        images_a = [
            # 2 described
            ArticleImage(
                article_id=art_a1.id, original_url="https://a/1.png",
                local_filename="1.png", local_path="/tmp/1.png",
                description="d1", is_meaningful=True,
            ),
            ArticleImage(
                article_id=art_a1.id, original_url="https://a/2.png",
                local_filename="2.png", local_path="/tmp/2.png",
                description="d2", is_meaningful=True,
            ),
            # 3 pending
            ArticleImage(
                article_id=art_a2.id, original_url="https://a/3.png",
                local_filename="3.png", local_path="/tmp/3.png",
                description=None, is_meaningful=None,
            ),
            ArticleImage(
                article_id=art_a2.id, original_url="https://a/4.png",
                local_filename="4.png", local_path="/tmp/4.png",
                description=None, is_meaningful=None,
            ),
            ArticleImage(
                article_id=art_a2.id, original_url="https://a/5.png",
                local_filename="5.png", local_path="/tmp/5.png",
                description=None, is_meaningful=True,
            ),
            # 1 decorative — excluded from both described and pending
            ArticleImage(
                article_id=art_a2.id, original_url="https://a/6.png",
                local_filename="6.png", local_path="/tmp/6.png",
                description=None, is_meaningful=False,
            ),
        ]
        s.add_all(images_a); await s.flush()

        run_a = ExtractionRun(
            source_id=src_a.id, status=RunStatus.COMPLETED,
            articles_extracted=3, articles_updated=1, articles_unchanged=200,
        )
        run_b = ExtractionRun(
            source_id=src_b.id, status=RunStatus.COMPLETED,
            articles_extracted=0, articles_updated=0, articles_unchanged=0,
            escalation_pending=[
                {"article_id": "x1", "page_start": 1, "page_end": 2, "level": 1, "title": "seg1"},
                {"article_id": "x2", "page_start": 3, "page_end": 4, "level": 1, "title": "seg2"},
                {"article_id": "x3", "page_start": 5, "page_end": 6, "level": 1, "title": "seg3"},
            ],
        )
        s.add_all([run_a, run_b]); await s.flush()

        await s.commit()
        return src_a.id, src_b.id, src_c.id


async def test_overview_shape_and_signals(ctx):
    c, factory = ctx
    a, b, cid = await _seed_overview(factory)
    resp = await c.get("/api/dashboard/overview")
    assert resp.status_code == 200
    body = resp.json()
    rows = {r["id"]: r for r in body["sources"]}

    ra = rows[str(a)]
    assert ra["source_type"] == "web" and ra["article_count"] == 2
    assert ra["last_run"]["new"] == 3 and ra["last_run"]["updated"] == 1
    assert ra["enrichment"] == {"described": 2, "pending": 3}   # decorative excluded
    assert ra["escalation"]["warning"] is False
    assert ra["escalation"]["run_id"] is None and ra["escalation"]["pending_count"] == 0

    rb = rows[str(b)]
    assert rb["source_type"] == "pdf"
    assert rb["escalation"]["warning"] is True
    assert rb["escalation"]["pending_count"] == 3
    assert rb["escalation"]["run_id"] is not None            # the run to retry
    assert rb["enrichment"] == {"described": 0, "pending": 0}

    rc = rows[str(cid)]
    assert rc["last_extracted_at"] is None and rc["last_run"] is None

    agg = body["aggregate"]
    assert agg["total"] == 3 and agg["never_extracted"] == 1
    assert agg["escalation_sources_with_warning"] == 1
    assert agg["enrichment"]["sources_with_backlog"] == 1     # only A


async def test_running_aggregate_matches_active_run(ctx):
    """The `running` aggregate counts the same active set as row.active_run, so
    the Running tile count always matches the rows its filter shows. A source
    with an in-flight run but status != EXTRACTING must still be counted."""
    c, factory = ctx
    async with factory() as s:
        from app.models.source import SourceStatus
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(
            name="R", base_url="https://r", product_id=p.id,
            source_type="web", status=SourceStatus.COMPLETED,  # not EXTRACTING
        )
        s.add(src); await s.flush()
        s.add(ExtractionRun(source_id=src.id, status=RunStatus.RUNNING))
        await s.commit()
        sid = src.id

    body = (await c.get("/api/dashboard/overview")).json()
    row = next(r for r in body["sources"] if r["id"] == str(sid))
    assert row["active_run"] is True
    assert body["aggregate"]["running"] == 1


async def test_overview_rbac_scopes_to_visible_vendors(ctx):
    """A principal restricted to one vendor sees only that vendor's source, and
    its per-source counts are still attached — i.e. the vendor-scoped helper
    scans don't drop counts for the visible source."""
    c, factory = ctx
    from app.core.authz import Principal, get_principal
    from app.models.source import SourceStatus
    from app.models.user import UserRole
    from app.models.user_vendor_permission import VendorAccessLevel

    async with factory() as s:
        v1 = Vendor(name="V1"); v2 = Vendor(name="V2")
        s.add_all([v1, v2]); await s.flush()
        p1 = Product(name="P1", vendor_id=v1.id); p2 = Product(name="P2", vendor_id=v2.id)
        s.add_all([p1, p2]); await s.flush()
        s1 = DocumentationSource(name="S1", base_url="https://s1", product_id=p1.id,
                                 source_type="web", status=SourceStatus.COMPLETED)
        s2 = DocumentationSource(name="S2", base_url="https://s2", product_id=p2.id,
                                 source_type="web", status=SourceStatus.COMPLETED)
        s.add_all([s1, s2]); await s.flush()
        art1 = Article(source_id=s1.id, title="a", source_url="https://s1/a",
                       topic_key="https://s1/a", content_markdown="#a", content_hash="h1")
        art2 = Article(source_id=s2.id, title="b", source_url="https://s2/b",
                       topic_key="https://s2/b", content_markdown="#b", content_hash="h2")
        s.add_all([art1, art2]); await s.flush()
        s.add_all([
            ArticleImage(article_id=art1.id, original_url="https://s1/1.png",
                         local_filename="1.png", local_path="/tmp/1.png",
                         description=None, is_meaningful=None),
            ArticleImage(article_id=art2.id, original_url="https://s2/2.png",
                         local_filename="2.png", local_path="/tmp/2.png",
                         description=None, is_meaningful=None),
        ])
        await s.commit()
        v1_id = v1.id

    app.dependency_overrides[get_principal] = lambda: Principal(
        see_all=False, role=UserRole.READ_ONLY,
        vendor_levels={v1_id: VendorAccessLevel.READ_ONLY},
    )
    try:
        body = (await c.get("/api/dashboard/overview")).json()
    finally:
        del app.dependency_overrides[get_principal]

    assert [r["name"] for r in body["sources"]] == ["S1"]        # V2's S2 hidden
    assert body["sources"][0]["article_count"] == 1
    assert body["sources"][0]["enrichment"] == {"described": 0, "pending": 1}
    assert body["aggregate"]["total"] == 1
    assert body["aggregate"]["enrichment"]["pending"] == 1       # V2's image excluded
