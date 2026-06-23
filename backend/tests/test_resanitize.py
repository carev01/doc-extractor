"""Tests for the re-sanitize backfill endpoint (POST /api/extraction/resanitize)."""

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
from app.models import (
    Vendor, Product, DocumentationSource, Article, ExtractionRun,
)
from app.models.article_version import ArticleVersion
from app.models.extraction_run import RunStatus
from app.services.firecrawl import compute_content_hash

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

# A leading Intercom font/Apache-license preamble that the sanitizer strips.
DIRTY = (
    'Copyright 2023. Intercom Inc. Licensed under the Apache License, Version 2.0 '
    '. See the License for the specific language. This Font Software is licensed '
    'under the SIL Open Font License, Version 1.1.'
    '[Skip to main content](https://help.example.com/x#main-content)\n'
    "\n# Real Title\n\nReal body content.\n"
)
CLEAN = "# Already Clean\n\nNothing to strip here.\n"


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


async def _source(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="Acme"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Cloud"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Docs", base_url="https://d.acme.com")
        s.add(src); await s.commit()
        return src.id


async def _article(factory, sid, md) -> uuid.UUID:
    async with factory() as s:
        a = Article(
            source_id=sid, title="t", source_url="https://d.acme.com/a",
            content_markdown=md, content_hash=compute_content_hash(md),
            sort_order=0, estimated_tokens=len(md) // 4,
            content_size_bytes=len(md.encode("utf-8")),
        )
        s.add(a); await s.commit()
        return a.id


async def test_resanitize_heals_dirty_article_and_records_version(client):
    c, factory = client
    sid = await _source(factory)
    aid = await _article(factory, sid, DIRTY)

    body = (await c.post(f"/api/extraction/resanitize/{sid}")).json()
    assert body == {"source_id": str(sid), "total": 1, "changed": 1, "unchanged": 0}

    async with factory() as s:
        art = await s.get(Article, aid)
        assert "Apache License" not in art.content_markdown
        assert art.content_markdown.startswith("# Real Title")
        assert art.content_hash == compute_content_hash(art.content_markdown)
        # the pre-sanitize content is preserved as an audit version
        versions = (
            await s.execute(select(ArticleVersion).where(ArticleVersion.article_id == aid))
        ).scalars().all()
        assert len(versions) == 1
        assert "Apache License" in versions[0].content_markdown
        assert versions[0].extraction_run_id is None
        assert versions[0].diff_text  # a diff was computed


async def test_resanitize_is_idempotent(client):
    c, factory = client
    sid = await _source(factory)
    await _article(factory, sid, DIRTY)

    first = (await c.post(f"/api/extraction/resanitize/{sid}")).json()
    assert first["changed"] == 1
    second = (await c.post(f"/api/extraction/resanitize/{sid}")).json()
    assert second == {"source_id": str(sid), "total": 1, "changed": 0, "unchanged": 1}

    # no spurious second version
    async with factory() as s:
        count = len((await s.execute(select(ArticleVersion))).scalars().all())
        assert count == 1


async def test_resanitize_clean_article_is_noop(client):
    c, factory = client
    sid = await _source(factory)
    await _article(factory, sid, CLEAN)
    body = (await c.post(f"/api/extraction/resanitize/{sid}")).json()
    assert body["changed"] == 0 and body["unchanged"] == 1
    async with factory() as s:
        assert (await s.execute(select(ArticleVersion))).scalars().first() is None


async def test_resanitize_rejected_while_run_active(client):
    c, factory = client
    sid = await _source(factory)
    await _article(factory, sid, DIRTY)
    async with factory() as s:
        s.add(ExtractionRun(source_id=sid, status=RunStatus.RUNNING))
        await s.commit()
    resp = await c.post(f"/api/extraction/resanitize/{sid}")
    assert resp.status_code == 409


async def test_resanitize_unknown_source_is_404(client):
    c, factory = client
    assert (await c.post(f"/api/extraction/resanitize/{uuid.uuid4()}")).status_code == 404
