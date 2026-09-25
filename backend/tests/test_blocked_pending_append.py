"""A still-blocked page must be recorded on the run, whatever the column held.

Every blocked-page retry ever run (9, from 2026-07-13) reported "0 still blocked"
while its pages actually failed. retry_blocked clears the carried list with
``run.blocked_pending = None`` — and a plain JSONB column persists Python None as
a JSON ``null``, not SQL NULL. The append that records a blocked page used
``COALESCE(blocked_pending, '[]')``, which only replaces SQL NULL, so the JSON
null reached ``jsonb_array_length`` and raised "cannot get array length of a
scalar". The page-level handler swallowed that, the page vanished from the
retry list, and the run completed as a success.
"""
import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import DocumentationSource, ExtractionRun, Product, Vendor
from app.services.firecrawl import _BLOCKED_PENDING_CAP, firecrawl_service

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

AKAMAI = (
    "Access Denied\n\nYou don't have permission to access this server.\n\n"
    "Reference #18.abc\n\nhttps://errors.edgesuite.net/18.abc\n"
)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source_and_run(f, kind="retry_blocked"):
    async with f() as s:
        v = Vendor(name="Commvault"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="CommCell"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Console",
                                  base_url="https://documentation.commvault.com/11.46/x")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, kind=kind); s.add(run); await s.commit()
        return src.id, run.id


async def _block(f, src_id, run_id, url):
    async with f() as db:
        return await firecrawl_service.process_article_result(
            db=db, source_id=src_id, run_id=run_id, url=url,
            markdown_content=AKAMAI, doc_html="<html>Access Denied</html>",
            toc_entry_id=None, sort_order=0, title="Topic",
        )


async def _pending(f, run_id):
    async with f() as db:
        return (await db.execute(text(
            "SELECT jsonb_typeof(blocked_pending), blocked_pending FROM extraction_runs WHERE id = :r"
        ), {"r": run_id})).one()


async def test_clearing_the_list_writes_sql_null_not_json_null(factory):
    """The root cause: None must persist as SQL NULL."""
    _, run_id = await _source_and_run(factory)
    async with factory() as db:
        run = await db.get(ExtractionRun, run_id)
        run.blocked_pending = [{"url": "https://x/a"}]
        await db.commit()
        run.blocked_pending = None          # what retry_blocked does before re-scraping
        await db.commit()
    kind, _ = await _pending(factory, run_id)
    assert kind is None, f"blocked_pending persisted as JSON {kind!r}, not SQL NULL"


async def test_a_retry_records_a_page_that_is_still_blocked(factory):
    """The live bug, end to end: clear the list the way retry_blocked does, then a
    still-blocked page must land in it — not raise and vanish."""
    src_id, run_id = await _source_and_run(factory)
    async with factory() as db:
        run = await db.get(ExtractionRun, run_id)
        run.blocked_pending = None
        await db.commit()
    url = "https://documentation.commvault.com/11.46/commcell-console/vnu0003.html"
    assert await _block(factory, src_id, run_id, url) == "blocked"
    kind, pending = await _pending(factory, run_id)
    assert kind == "array" and [p["url"] for p in pending] == [url]


async def test_an_existing_json_null_does_not_break_the_append(factory):
    """Rows written before the fix already hold a JSON null (all 9 retry runs in
    production); the append must treat any non-array as an empty list."""
    src_id, run_id = await _source_and_run(factory)
    async with factory() as db:
        await db.execute(text("UPDATE extraction_runs SET blocked_pending = 'null'::jsonb WHERE id = :r"),
                         {"r": run_id})
        await db.commit()
    url = "https://documentation.commvault.com/11.46/commcell-console/vnu0003_01.html"
    assert await _block(factory, src_id, run_id, url) == "blocked"
    kind, pending = await _pending(factory, run_id)
    assert kind == "array" and [p["url"] for p in pending] == [url]


async def test_any_other_scalar_is_replaced_too(factory):
    src_id, run_id = await _source_and_run(factory)
    async with factory() as db:
        await db.execute(text("UPDATE extraction_runs SET blocked_pending = '{\"oops\": 1}'::jsonb WHERE id = :r"),
                         {"r": run_id})
        await db.commit()
    assert await _block(factory, src_id, run_id, "https://x/p") == "blocked"
    kind, pending = await _pending(factory, run_id)
    assert kind == "array" and [p["url"] for p in pending] == ["https://x/p"]


async def test_the_same_page_is_recorded_once(factory):
    src_id, run_id = await _source_and_run(factory, kind="extract")
    for _ in range(3):
        await _block(factory, src_id, run_id, "https://x/dup")
    _, pending = await _pending(factory, run_id)
    assert [p["url"] for p in pending] == ["https://x/dup"]


async def test_the_list_stays_capped(factory):
    src_id, run_id = await _source_and_run(factory, kind="extract")
    async with factory() as db:
        await db.execute(text(
            "UPDATE extraction_runs SET blocked_pending = "
            "(SELECT jsonb_agg(jsonb_build_object('url', 'https://x/' || g)) FROM generate_series(1, :cap) g) "
            "WHERE id = :r"), {"cap": _BLOCKED_PENDING_CAP, "r": run_id})
        await db.commit()
    await _block(factory, src_id, run_id, "https://x/overflow")
    _, pending = await _pending(factory, run_id)
    assert len(pending) == _BLOCKED_PENDING_CAP
    assert "https://x/overflow" not in [p["url"] for p in pending]
