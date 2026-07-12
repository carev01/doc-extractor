"""Resumable bootstrap: bootstrap_after + bootstrap_start watermark."""
import json
import os
import sys

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
from app.models import Vendor, Product, DocumentationSource, Article

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


async def _seed_n(factory, n):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        arts = [Article(source_id=src.id, title=str(i), source_url=f"https://s/{i}",
                        topic_key=f"https://s/{i}", content_markdown=f"#{i}", content_hash=f"h{i}")
                for i in range(n)]
        s.add_all(arts); await s.commit()
        return src.id


def _lines(text):
    return [json.loads(l) for l in text.splitlines() if l]


async def test_bootstrap_start_line_matches_terminal_cursor(ctx):
    c, factory = ctx
    await _seed_n(factory, 3)
    lines = _lines((await c.get("/api/articles/delta")).text)
    assert lines[0].get("control") == "bootstrap_start"
    assert lines[-1].get("control") == "cursor"
    assert lines[0]["next_since"] == lines[-1]["next_since"]
    added = [l for l in lines if l.get("change_type") == "added"]
    assert len(added) == 3


async def test_bootstrap_after_filters_and_is_gapless(ctx):
    c, factory = ctx
    await _seed_n(factory, 3)
    full = [l for l in _lines((await c.get("/api/articles/delta")).text) if l.get("change_type") == "added"]
    ids = [l["id"] for l in full]                       # ascending by Article.id
    rest = [l for l in _lines((await c.get(f"/api/articles/delta?bootstrap_after={ids[0]}")).text)
            if l.get("change_type") == "added"]
    assert [l["id"] for l in rest] == ids[1:]           # first one skipped, no dupes/gaps


async def test_resume_with_original_watermark_catches_missed_update(ctx):
    """Using the FIRST attempt's watermark (not the resume-time one) means an
    update to an already-emitted article is still served by incremental."""
    c, factory = ctx
    src_id = await _seed_n(factory, 2)
    lines = _lines((await c.get("/api/articles/delta")).text)
    x = lines[0]["next_since"]                          # first-attempt watermark
    added = [l for l in lines if l.get("change_type") == "added"]
    a0 = added[0]["id"]

    # Update the already-emitted first article (writes a content_changes row).
    async with factory() as s:
        art = (await s.execute(select(Article).where(Article.id == a0))).scalar_one()
        art.content_markdown = "# changed"
        from app.services import change_log
        # simulate an extraction updated-row for this article
        await change_log.record_change(s, article=art, change_type="updated", run_id=None)
        await s.commit()

    # Resume bootstrap past a0 (recomputed watermark would MISS the update)...
    _resume = await c.get(f"/api/articles/delta?bootstrap_after={a0}")
    # ...but incremental from the ORIGINAL watermark x still serves the update.
    inc = _lines((await c.get(f"/api/articles/delta?since={x}")).text)
    updated_ids = [l["id"] for l in inc if l.get("change_type") == "updated"]
    assert a0 in updated_ids
