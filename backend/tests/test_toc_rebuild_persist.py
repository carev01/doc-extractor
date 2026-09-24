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


# ── Re-linking articles to a rebuilt TOC ─────────────────────────────────────
# The batched re-link used to be inline in extract_source, so nothing exercised
# it: it raised on every oxygen_webhelp run from f5e1848 (2026-06-29) on, and the
# run only came out right because nothing rolled back and _reconcile_removals
# re-linked by URL afterwards.

from sqlalchemy import bindparam, update  # noqa: E402

from app.models import Article  # noqa: E402


async def _article(db, source_id, url):
    a = Article(
        source_id=source_id, title=url.rsplit("/", 1)[-1], source_url=url,
        topic_key=url, content_markdown="body", content_hash=url, sort_order=0,
    )
    db.add(a)
    await db.flush()
    return a


def _entries(*urls, parent_first=True):
    return [
        {"title": u.rsplit("/", 1)[-1], "url": u, "level": 0, "is_article": True,
         "parent_url": None, "sort_order": i}
        for i, u in enumerate(urls)
    ]


@pytest.mark.asyncio
async def test_the_original_statement_raises_without_synchronize_session(db):
    """Pins why the re-link is a Core statement — this is the exact error from
    the Rubrik run log, raised before any SQL is sent."""
    sid = await _source(db)
    await _article(db, sid, "http://x/a")
    with pytest.raises(Exception, match="bulk synchronize of persistent objects"):
        await db.execute(
            update(Article)
            .where(Article.source_id == sid, Article.source_url == bindparam("b_url"))
            .values(toc_entry_id=bindparam("b_tid")),
            [{"b_url": "http://x/a", "b_tid": uuid.uuid4()}],
        )


@pytest.mark.asyncio
async def test_relink_points_articles_at_their_new_entries(db):
    sid = await _source(db)
    a = await _article(db, sid, "http://x/a")
    b = await _article(db, sid, "http://x/b")
    url_to_id = await firecrawl_service._persist_toc(db, sid, _entries("http://x/a", "http://x/b"))

    await firecrawl_service._relink_articles_to_toc(db, sid, url_to_id)
    await db.commit()

    rows = dict((await db.execute(
        select(Article.id, Article.toc_entry_id).where(Article.source_id == sid)
    )).all())
    assert rows[a.id] == url_to_id["http://x/a"]
    assert rows[b.id] == url_to_id["http://x/b"]


@pytest.mark.asyncio
async def test_a_reused_topic_links_to_its_last_occurrence(db):
    """Oxygen lists a reused topic under each parent, so one URL has several
    entries. The map keeps the last, and that is where the article lands."""
    sid = await _source(db)
    art = await _article(db, sid, "http://x/reused")
    toc = _entries("http://x/reused", "http://x/other", "http://x/reused")
    url_to_id = await firecrawl_service._persist_toc(db, sid, toc)

    await firecrawl_service._relink_articles_to_toc(db, sid, url_to_id)
    await db.commit()

    last = (await db.execute(
        select(TOCEntry.id).where(TOCEntry.source_id == sid, TOCEntry.url == "http://x/reused")
        .order_by(TOCEntry.sort_order.desc()).limit(1)
    )).scalar_one()
    linked = (await db.execute(select(Article.toc_entry_id).where(Article.id == art.id))).scalar_one()
    assert linked == last


@pytest.mark.asyncio
async def test_relink_never_touches_another_sources_articles(db):
    sid, other = await _source(db), await _source(db)
    await _article(db, sid, "http://x/a")
    foreign = await _article(db, other, "http://x/a")  # same URL, different source
    url_to_id = await firecrawl_service._persist_toc(db, sid, _entries("http://x/a"))

    await firecrawl_service._relink_articles_to_toc(db, sid, url_to_id)
    await db.commit()

    assert (await db.execute(
        select(Article.toc_entry_id).where(Article.id == foreign.id)
    )).scalar_one() is None


@pytest.mark.asyncio
async def test_an_empty_map_is_a_no_op(db):
    sid = await _source(db)
    await firecrawl_service._relink_articles_to_toc(db, sid, {})


async def _inventory(db, sid, *urls):
    """A committed inventory TOC with its articles linked, as phase 1 leaves it."""
    arts = [await _article(db, sid, u) for u in urls]
    url_to_id = await firecrawl_service._persist_toc(db, sid, _entries(*urls))
    await firecrawl_service._relink_articles_to_toc(db, sid, url_to_id)
    await db.commit()
    return [a.id for a in arts], url_to_id


@pytest.mark.asyncio
async def test_apply_rebuilt_toc_swaps_the_toc_and_relinks(db):
    sid = await _source(db)
    (a_id,), _ = await _inventory(db, sid, "http://x/a")

    ok = await firecrawl_service._apply_rebuilt_toc(
        db, sid, _entries("http://x/root", "http://x/a"))

    assert ok is True
    urls = (await db.execute(
        select(TOCEntry.url).where(TOCEntry.source_id == sid).order_by(TOCEntry.sort_order)
    )).scalars().all()
    assert urls == ["http://x/root", "http://x/a"]
    new_a = (await db.execute(
        select(TOCEntry.id).where(TOCEntry.source_id == sid, TOCEntry.url == "http://x/a")
    )).scalar_one()
    assert (await db.execute(
        select(Article.toc_entry_id).where(Article.id == a_id)
    )).scalar_one() == new_a


@pytest.mark.asyncio
async def test_a_failed_relink_keeps_the_inventory_toc_and_its_links(db, monkeypatch):
    """What the log line promises. Before, the inventory TOC was deleted and the
    rebuilt one inserted-but-unlinked, and a later commit saved that."""
    sid = await _source(db)
    (a_id,), inventory = await _inventory(db, sid, "http://x/a")

    async def boom(*_a, **_kw):
        raise RuntimeError("relink failed")
    monkeypatch.setattr(firecrawl_service, "_relink_articles_to_toc", boom)

    ok = await firecrawl_service._apply_rebuilt_toc(
        db, sid, _entries("http://x/root", "http://x/a"))
    await db.commit()  # the next unrelated commit in extract_source

    assert ok is False
    urls = (await db.execute(
        select(TOCEntry.url).where(TOCEntry.source_id == sid)
    )).scalars().all()
    assert urls == ["http://x/a"], "the inventory TOC must survive a failed rebuild"
    assert (await db.execute(
        select(Article.toc_entry_id).where(Article.id == a_id)
    )).scalar_one() == inventory["http://x/a"]


@pytest.mark.asyncio
async def test_a_failed_rebuild_leaves_other_orm_objects_usable(db, monkeypatch):
    """The regression a plain db.rollback() would have introduced: it expires
    every instance, and extract_source reads run/source attributes right after,
    which on an AsyncSession is implicit IO -> MissingGreenlet. The savepoint
    must leave an object loaded beforehand readable without a reload."""
    sid = await _source(db)
    await _inventory(db, sid, "http://x/a")
    source = await db.get(DocumentationSource, sid)

    async def boom(*_a, **_kw):
        raise RuntimeError("relink failed")
    monkeypatch.setattr(firecrawl_service, "_relink_articles_to_toc", boom)

    assert await firecrawl_service._apply_rebuilt_toc(db, sid, _entries("http://x/b")) is False
    assert source.base_url == "http://x"  # would raise MissingGreenlet if expired


@pytest.mark.asyncio
async def test_the_suggested_execution_option_is_not_enough(db):
    """The error message suggests synchronize_session=None. That only swaps one
    failure for another: an ORM UPDATE given a parameter list is a bulk update
    BY PRIMARY KEY, so it can't key on URL whatever the option says."""
    sid = await _source(db)
    await _article(db, sid, "http://x/a")
    with pytest.raises(Exception, match="No primary key value supplied"):
        await db.execute(
            update(Article)
            .where(Article.source_id == sid, Article.source_url == bindparam("b_url"))
            .values(toc_entry_id=bindparam("b_tid"))
            .execution_options(synchronize_session=None),
            [{"b_url": "http://x/a", "b_tid": uuid.uuid4()}],
        )
