"""Deleting a source, product or vendor is one statement; Postgres cascades.

Every FK below a vendor is ON DELETE CASCADE in the database (vendor -> products
-> sources -> runs / articles / TOC entries; articles -> images / versions). The
ORM relationships mirrored that with cascade="all, delete-orphan" but without
passive_deletes, so SQLAlchemy loaded every child and deleted it one row at a
time. Deleting one source with 9,592 TOC entries took ~7 minutes; the request
timed out in the browser long before it finished. These tests pin both the end
state and the shape: no per-row child DELETEs.
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import (
    Article, ArticleVersion, DocumentationSource, ExtractionRun, Product, Vendor,
)
from app.models.content_change import ContentChange
from app.models.image import ArticleImage
from app.models.toc import TOCEntry

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()))

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, factory, statements
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(factory, n_articles=3, n_toc=5):
    """Vendor -> product -> source with runs, a nested TOC, articles, images and
    versions — one of every row type the cascade has to reach."""
    async with factory() as s:
        v = Vendor(name=f"V-{uuid.uuid4().hex[:6]}"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="S", base_url="https://d/x")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id); s.add(run); await s.flush()
        parent = None
        for i in range(n_toc):
            t = TOCEntry(source_id=src.id, title=f"T{i}", url=f"https://d/{i}",
                         level=0 if parent is None else 1, sort_order=i,
                         parent_id=parent.id if parent else None)
            s.add(t); await s.flush()
            parent = parent or t
        for i in range(n_articles):
            a = Article(source_id=src.id, title=f"A{i}", source_url=f"https://d/{i}",
                        topic_key=f"https://d/{i}", content_markdown="body",
                        content_hash=f"h{i}", sort_order=i, toc_entry_id=parent.id)
            s.add(a); await s.flush()
            s.add(ArticleImage(article_id=a.id, original_url=f"https://d/{i}.png",
                               local_filename=f"{i}.png", local_path=f"/media/{i}.png"))
            s.add(ArticleVersion(article_id=a.id, content_markdown="old", content_hash=f"o{i}"))
        await s.commit()
        return v.id, p.id, src.id


async def _count(factory, model, **where):
    async with factory() as s:
        q = select(func.count()).select_from(model)
        for k, val in where.items():
            q = q.where(getattr(model, k) == val)
        return (await s.execute(q)).scalar_one()


_TREE = ("vendors", "products", "documentation_sources", "extraction_runs", "articles",
         "toc_entries", "article_images", "article_versions")


def _deletes(statements):
    """Table name of every DELETE issued within the vendor -> ... -> article tree."""
    return [st.split()[2].strip('"') for st in statements
            if st.upper().startswith("DELETE FROM") and st.split()[2].strip('"') in _TREE]


def _assert_single_delete(statements, top):
    """Exactly one DELETE, on *top*; every child is left to the database."""
    assert _deletes(statements) == [top], (
        f"expected one DELETE on {top} with the rest cascaded by Postgres, got "
        f"{_deletes(statements)} — is the ORM deleting children row by row again?")


async def test_deleting_a_source_is_one_statement_and_cascades(env):
    c, factory, statements = env
    _, _, sid = await _seed(factory)
    statements.clear()

    r = await c.delete(f"/api/sources/{sid}")
    assert r.status_code == 204

    _assert_single_delete(statements, "documentation_sources")
    for model in (Article, TOCEntry, ExtractionRun):
        assert await _count(factory, model, source_id=sid) == 0
    assert await _count(factory, ArticleImage) == 0
    assert await _count(factory, ArticleVersion) == 0
    assert await _count(factory, DocumentationSource, id=sid) == 0


async def test_the_outbox_removal_rows_survive_the_cascade(env):
    """content_changes has no FK to articles or sources by design, so the
    'removed' rows the delete writes for downstream consumers must outlive it."""
    c, factory, _ = env
    _, _, sid = await _seed(factory, n_articles=3)
    assert (await c.delete(f"/api/sources/{sid}")).status_code == 204
    assert await _count(factory, ContentChange, source_id=sid, change_type="removed") == 3


async def test_deleting_a_product_cascades_through_its_sources(env):
    c, factory, statements = env
    _, pid, sid = await _seed(factory)
    statements.clear()
    assert (await c.delete(f"/api/products/{pid}")).status_code == 204
    _assert_single_delete(statements, "products")
    assert await _count(factory, DocumentationSource, product_id=pid) == 0
    assert await _count(factory, Article, source_id=sid) == 0
    assert await _count(factory, TOCEntry, source_id=sid) == 0


async def test_deleting_a_vendor_cascades_all_the_way_down(env):
    c, factory, statements = env
    vid, pid, sid = await _seed(factory)
    statements.clear()
    assert (await c.delete(f"/api/vendors/{vid}")).status_code == 204
    _assert_single_delete(statements, "vendors")
    assert await _count(factory, Product, vendor_id=vid) == 0
    assert await _count(factory, Article, source_id=sid) == 0
    assert await _count(factory, ArticleVersion) == 0


async def test_toc_parent_id_is_indexed():
    """Every TOC-entry delete looks up its children through parent_id."""
    assert TOCEntry.__table__.c.parent_id.index is True
