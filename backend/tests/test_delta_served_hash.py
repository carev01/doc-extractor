"""Test that delta records' content_hash is sha256(content_markdown), not the Article's stored hash."""
import hashlib
import json
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
from app.models.content_change import ContentChange
from app.models.extraction_run import RunStatus

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


def _records(text_body):
    """Extract records from NDJSON body (excluding control line)."""
    lines = [json.loads(l) for l in text_body.splitlines() if l.strip()]
    # Last line is control
    control = lines[-1]
    assert control.get("control") == "cursor"
    return lines[:-1]


async def _seed_article(factory, *, content_markdown, content_hash):
    """Create source + run + article with given markdown and stored content_hash."""
    async with factory() as s:
        v = Vendor(name="V")
        s.add(v)
        await s.flush()
        p = Product(name="P", vendor_id=v.id)
        s.add(p)
        await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src)
        await s.flush()
        run = ExtractionRun(source_id=src.id, status=RunStatus.COMPLETED)
        s.add(run)
        await s.flush()
        art = Article(
            source_id=src.id,
            extraction_run_id=run.id,
            created_run_id=run.id,
            title="Test Article",
            source_url="https://x/test",
            topic_key="https://x/test",
            content_markdown=content_markdown,
            content_hash=content_hash,
        )
        s.add(art)
        await s.flush()
        art_id = art.id
        # Add a content_changes row so it appears in the delta feed
        s.add(
            ContentChange(
                article_id=art_id,
                source_id=src.id,
                run_id=run.id,
                change_type="added",
                content_hash=content_hash,
                topic_key="https://x/test",
            )
        )
        await s.commit()
        return art_id


async def test_delta_record_hash_is_of_served_markdown(ctx):
    """Assert the delta record's content_hash is sha256(content_markdown)."""
    c, factory = ctx
    md = "# A\n\n![p](/media/x/y.png)\n\n> **Figure:** A diagram.\n"
    await _seed_article(factory, content_markdown=md, content_hash="raw-fingerprint")

    resp = await c.get("/api/articles/delta")
    assert resp.status_code == 200

    records = _records(resp.text)
    rec = next(r for r in records if r.get("change_type") == "added")

    expected_hash = hashlib.sha256(md.encode("utf-8")).hexdigest()
    assert rec["content_hash"] == expected_hash
    assert rec["content_hash"] != "raw-fingerprint"
