"""Tests for FirecrawlService._persist_toc — delete+insert+parent-linkage helper.

Mirrors the style of tests/test_reconcile_removals.py (async asyncpg session).
Verifies:
  - Old TOCEntry rows for the source are deleted before insert.
  - Entries at level > 0 are linked to correct parent by parent_url / level.
  - The returned dict maps every non-None URL to its new TOCEntry UUID.
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource
from app.models.toc import TOCEntry
from app.services.firecrawl import firecrawl_service, _toc_superset
from app.services.profiles.base import TocEntry as ProfileTocEntry

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(db) -> uuid.UUID:
    v = Vendor(name=f"V-{uuid.uuid4().hex[:8]}")
    db.add(v)
    await db.flush()
    p = Product(vendor_id=v.id, name="P")
    db.add(p)
    await db.flush()
    s = DocumentationSource(product_id=p.id, name="S", base_url="http://x")
    db.add(s)
    await db.flush()
    return s.id


@pytest.mark.asyncio
async def test_persist_toc_deletes_old_entries(db):
    """Old TOCEntry rows for the source are removed before inserting the new tree."""
    source_id = await _source(db)
    old = TOCEntry(
        source_id=source_id, title="old", url="http://x/old",
        level=0, sort_order=0, is_article=True, parent_id=None,
    )
    db.add(old)
    await db.commit()

    toc_entries = [
        {
            "title": "Root", "url": "http://x/root", "level": 0,
            "is_article": True, "parent_url": None, "sort_order": 0,
        },
    ]
    await firecrawl_service._persist_toc(db, source_id, toc_entries)
    await db.commit()

    rows = (
        await db.execute(select(TOCEntry).where(TOCEntry.source_id == source_id))
    ).scalars().all()
    urls = {r.url for r in rows}
    assert "http://x/old" not in urls, "old entry must be deleted"
    assert "http://x/root" in urls, "new entry must be inserted"
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_persist_toc_parent_linkage(db):
    """Entries at level > 0 are linked to their correct parent via parent_url."""
    source_id = await _source(db)

    toc_entries = [
        {
            "title": "Root", "url": "http://x/root", "level": 0,
            "is_article": True, "parent_url": None, "sort_order": 0,
        },
        {
            "title": "Child A", "url": "http://x/child-a", "level": 1,
            "is_article": True, "parent_url": "http://x/root", "sort_order": 1,
        },
        {
            "title": "Grandchild", "url": "http://x/grandchild", "level": 2,
            "is_article": True, "parent_url": "http://x/child-a", "sort_order": 2,
        },
        {
            "title": "Child B", "url": "http://x/child-b", "level": 1,
            "is_article": True, "parent_url": "http://x/root", "sort_order": 3,
        },
    ]
    await firecrawl_service._persist_toc(db, source_id, toc_entries)
    await db.commit()

    rows = {
        r.url: r
        for r in (
            await db.execute(select(TOCEntry).where(TOCEntry.source_id == source_id))
        ).scalars().all()
    }

    assert rows["http://x/root"].parent_id is None
    assert rows["http://x/child-a"].parent_id == rows["http://x/root"].id
    assert rows["http://x/child-b"].parent_id == rows["http://x/root"].id
    assert rows["http://x/grandchild"].parent_id == rows["http://x/child-a"].id


@pytest.mark.asyncio
async def test_persist_toc_returns_url_to_id_map(db):
    """The returned dict maps every non-None entry URL to its new TOCEntry UUID.
    URL-less section headers (url=None or url='') must NOT appear in the map."""
    source_id = await _source(db)

    toc_entries = [
        {
            "title": "Page 1", "url": "http://x/p1", "level": 0,
            "is_article": True, "parent_url": None, "sort_order": 0,
        },
        {
            "title": "Page 2", "url": "http://x/p2", "level": 0,
            "is_article": True, "parent_url": None, "sort_order": 1,
        },
        {
            "title": "Section Header", "url": None, "level": 0,
            "is_article": False, "parent_url": None, "sort_order": 2,
        },
    ]
    url_to_id = await firecrawl_service._persist_toc(db, source_id, toc_entries)
    await db.commit()

    # URL-less header must NOT be in the map
    assert None not in url_to_id
    assert "" not in url_to_id

    # Article URLs must be present and map to valid UUIDs
    assert "http://x/p1" in url_to_id
    assert "http://x/p2" in url_to_id
    assert isinstance(url_to_id["http://x/p1"], uuid.UUID)
    assert isinstance(url_to_id["http://x/p2"], uuid.UUID)
    assert url_to_id["http://x/p1"] != url_to_id["http://x/p2"]

    # IDs in the map match what's in the DB
    rows = {
        r.url: r
        for r in (
            await db.execute(select(TOCEntry).where(TOCEntry.source_id == source_id))
        ).scalars().all()
        if r.url
    }
    assert rows["http://x/p1"].id == url_to_id["http://x/p1"]
    assert rows["http://x/p2"].id == url_to_id["http://x/p2"]


# ── FIX 1: superset helper unit test ────────────────────────────────────────

def test_toc_superset_adds_uncovered_scraped_urls():
    """FIX 1 (I-1): a scraped article whose URL is NOT among the rebuilt entries
    is appended as a flat extra by _toc_superset so _reconcile_removals won't
    mark it removed.

    This is a unit test of the pure helper (no DB required). Approach: helper.
    """
    rebuilt = [
        ProfileTocEntry(title="Root", url="http://x/root", level=0),
        ProfileTocEntry(title="Child", url="http://x/child", level=1,
                        parent_url="http://x/root"),
    ]
    # "http://x/orphan" was scraped but the rebuild_toc didn't include it
    scraped = [
        ("http://x/root", "Root"),
        ("http://x/child", "Child"),
        ("http://x/orphan", "Orphan Page"),
    ]
    result = _toc_superset(rebuilt, scraped)
    urls = [e.url for e in result]

    assert "http://x/orphan" in urls, (
        "scraped article not covered by rebuilt TOC must appear as a flat extra"
    )
    orphan = next(e for e in result if e.url == "http://x/orphan")
    assert orphan.level == 0, "extra entry must be flat (level 0)"
    assert orphan.is_article is True
    assert orphan.parent_url is None

    # Already-covered URLs must not be duplicated
    assert urls.count("http://x/root") == 1
    assert urls.count("http://x/child") == 1

    # Rebuilt entries come first; extras are appended after
    assert urls.index("http://x/root") < urls.index("http://x/orphan")
    assert urls.index("http://x/child") < urls.index("http://x/orphan")
