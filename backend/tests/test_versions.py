"""Tests for version-history and changelog routes.

Unlike the export/incremental tests (which use a sync session against the
data layer), these exercise the async FastAPI routes end-to-end via
httpx.AsyncClient with get_db overridden to point at docextractor_test.
The app lifespan is not triggered by ASGITransport, so the main-DB engine
is never touched.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, delete
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
    Vendor,
    DocumentationSource,
    Article,
    ArticleVersion,
    TOCEntry,
    ExtractionRun,
)
from app.models.extraction_run import RunStatus
from app.services.diffing import compute_unified_diff
from app.services.firecrawl import firecrawl_service

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def client():
    """Yield (AsyncClient, session_factory).

    The async engine is created per-test with NullPool so its connections bind
    to this test's event loop — pytest-asyncio gives each test its own loop, and
    a shared pooled engine would raise "another operation is in progress".
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session_factory
    app.dependency_overrides.clear()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(TestSession):
    """Create a source with one article that has two prior versions.

    Timeline: content "old" -> "mid" (snapshot v_old, no stored diff) ->
    "new" (snapshot v_mid, stored diff). Live article content is "new".
    Returns (source_id, article_id, v_old_id, v_mid_id).
    """
    async with TestSession() as s:
        vendor = Vendor(name="VerVendor")
        s.add(vendor)
        await s.flush()

        source = DocumentationSource(
            vendor_id=vendor.id, name="VerSource", base_url="https://docs.ver.com"
        )
        s.add(source)
        await s.flush()

        article = Article(
            source_id=source.id,
            title="Versioned Article",
            source_url="https://docs.ver.com/a",
            content_markdown="line one\nnew content\n",
            content_hash="current-hash",
            sort_order=0,
            estimated_tokens=10,
            content_size_bytes=20,
        )
        s.add(article)
        await s.flush()

        v_old = ArticleVersion(
            article_id=article.id,
            extraction_run_id=None,
            content_markdown="line one\nold content\n",
            content_hash="old-hash",
            diff_text=None,  # hash-path snapshot, no stored diff
            extracted_at=T0,
        )
        v_mid = ArticleVersion(
            article_id=article.id,
            extraction_run_id=None,
            content_markdown="line one\nmid content\n",
            content_hash="mid-hash",
            diff_text="STORED-DIFF-MID-TO-NEW",
            extracted_at=T0 + timedelta(days=1),
        )
        s.add_all([v_old, v_mid])
        await s.commit()
        return source.id, article.id, v_old.id, v_mid.id


# ── Diff helper (pure) ──

def test_compute_unified_diff_basic():
    diff = compute_unified_diff("a\nb\n", "a\nc\n")
    assert "-b" in diff
    assert "+c" in diff


def test_compute_unified_diff_identical_is_empty():
    assert compute_unified_diff("same\n", "same\n") == ""


# ── Version list ──

async def test_list_versions_newest_first(client):
    client, TestSession = client
    source_id, article_id, v_old_id, v_mid_id = await _seed(TestSession)

    resp = await client.get(f"/api/articles/{article_id}/versions")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 2
    assert body["current_hash"] == "current-hash"
    ids = [v["id"] for v in body["versions"]]
    assert ids == [str(v_mid_id), str(v_old_id)]  # newest (later) first

    by_id = {v["id"]: v for v in body["versions"]}
    assert by_id[str(v_mid_id)]["has_diff"] is True
    assert by_id[str(v_old_id)]["has_diff"] is False
    assert by_id[str(v_old_id)]["content_size_bytes"] > 0


async def test_list_versions_unknown_article_404(client):
    client, _ = client
    resp = await client.get(f"/api/articles/{uuid.uuid4()}/versions")
    assert resp.status_code == 404


# ── Version detail ──

async def test_get_version_detail_returns_body(client):
    client, TestSession = client
    _, article_id, v_old_id, _ = await _seed(TestSession)
    resp = await client.get(f"/api/articles/{article_id}/versions/{v_old_id}")
    assert resp.status_code == 200
    assert resp.json()["content_markdown"] == "line one\nold content\n"


async def test_get_version_mismatched_article_404(client):
    client, TestSession = client
    _, article_id, _, v_mid_id = await _seed(TestSession)
    # valid version id but wrong article id
    resp = await client.get(f"/api/articles/{uuid.uuid4()}/versions/{v_mid_id}")
    assert resp.status_code == 404


# ── Diff ──

async def test_diff_uses_stored_diff_for_latest_version(client):
    client, TestSession = client
    _, article_id, _, v_mid_id = await _seed(TestSession)
    resp = await client.get(
        f"/api/articles/{article_id}/versions/{v_mid_id}/diff"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["computed"] is False
    assert body["diff_text"] == "STORED-DIFF-MID-TO-NEW"
    assert body["to_label"] == "current"  # no newer version exists


async def test_diff_computes_against_next_version_when_no_stored_diff(client):
    client, TestSession = client
    _, article_id, v_old_id, v_mid_id = await _seed(TestSession)
    resp = await client.get(
        f"/api/articles/{article_id}/versions/{v_old_id}/diff"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["computed"] is True
    # old -> mid transition
    assert "-old content" in body["diff_text"]
    assert "+mid content" in body["diff_text"]
    assert body["to_label"] == f"version:{v_mid_id}"


async def test_diff_against_current_overrides_next(client):
    client, TestSession = client
    _, article_id, v_old_id, _ = await _seed(TestSession)
    resp = await client.get(
        f"/api/articles/{article_id}/versions/{v_old_id}/diff?against=current"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["computed"] is True
    assert body["to_label"] == "current"
    assert "-old content" in body["diff_text"]
    assert "+new content" in body["diff_text"]


# ── Changelog ──

async def test_source_changelog_newest_first_across_articles(client):
    client, TestSession = client
    source_id, article_id, v_old_id, v_mid_id = await _seed(TestSession)

    # Add a second article with one (more recent) version.
    async with TestSession() as s:
        article2 = Article(
            source_id=source_id,
            title="Second Article",
            source_url="https://docs.ver.com/b",
            content_markdown="b new\n",
            content_hash="b-current",
            sort_order=1,
            estimated_tokens=5,
            content_size_bytes=10,
        )
        s.add(article2)
        await s.flush()
        s.add(ArticleVersion(
            article_id=article2.id,
            content_markdown="b old\n",
            content_hash="b-old",
            diff_text="B-DIFF",
            extracted_at=T0 + timedelta(days=2),
        ))
        await s.commit()

    resp = await client.get(f"/api/sources/{source_id}/changelog")
    assert resp.status_code == 200
    body = resp.json()
    # These articles have no created_run_id, so they produce no 'added'/'initial'
    # events — only the 3 content-change ("changed") events appear.
    assert body["total"] == 3
    changed = [e for e in body["entries"] if e["change_type"] == "changed"]
    assert len(changed) == 3
    # Among content changes: most recent (Second Article @ day2) first, oldest last.
    assert changed[0]["title"] == "Second Article"
    assert changed[-1]["version_id"] == str(v_old_id)


async def test_changelog_unknown_source_404(client):
    client, _ = client
    resp = await client.get(f"/api/sources/{uuid.uuid4()}/changelog")
    assert resp.status_code == 404


# ── Browse (annotated TOC + removed) ──

RUN_START = T0 + timedelta(days=10)


async def _seed_browse(TestSession):
    """Seed a source with a section + articles covering every change_status.

    Returns dict of useful ids. Timeline: a completed run started at RUN_START.
      - updated: created before the run, has a version snapshot from this run
      - new: created during the run
      - unchanged: created before the run, no version this run
      - removed: orphaned (toc_entry_id is None)
    """
    async with TestSession() as s:
        vendor = Vendor(name="BrowseVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="BrowseSource", base_url="https://b.com"
        )
        s.add(source)
        await s.flush()

        run = ExtractionRun(
            source_id=source.id, status=RunStatus.COMPLETED, started_at=RUN_START
        )
        s.add(run)
        await s.flush()

        section = TOCEntry(
            source_id=source.id, title="Section", url=None, level=0,
            sort_order=0, is_article=False,
        )
        s.add(section)
        await s.flush()

        toc_updated = TOCEntry(
            source_id=source.id, title="Updated Page", url="https://b.com/u",
            level=1, sort_order=1, is_article=True, parent_id=section.id,
        )
        toc_new = TOCEntry(
            source_id=source.id, title="New Page", url="https://b.com/n",
            level=1, sort_order=2, is_article=True, parent_id=section.id,
        )
        toc_unchanged = TOCEntry(
            source_id=source.id, title="Stable Page", url="https://b.com/s",
            level=1, sort_order=3, is_article=True, parent_id=section.id,
        )
        s.add_all([toc_updated, toc_new, toc_unchanged])
        await s.flush()

        def mk(title, url, toc_id, created_at):
            return Article(
                source_id=source.id, toc_entry_id=toc_id, title=title,
                source_url=url, content_markdown="x", content_hash="h",
                sort_order=0, estimated_tokens=1, content_size_bytes=1,
                created_at=created_at,
            )

        a_updated = mk("Updated Page", "https://b.com/u", toc_updated.id, T0)
        a_new = mk("New Page", "https://b.com/n", toc_new.id, RUN_START + timedelta(minutes=5))
        a_unchanged = mk("Stable Page", "https://b.com/s", toc_unchanged.id, T0)
        a_removed = mk("Gone Page", "https://b.com/gone", None, T0)
        s.add_all([a_updated, a_new, a_unchanged, a_removed])
        await s.flush()

        # The updated page got a version snapshot from this run.
        s.add(ArticleVersion(
            article_id=a_updated.id, extraction_run_id=run.id,
            content_markdown="old", content_hash="old", extracted_at=RUN_START,
        ))
        await s.commit()

        return {
            "source_id": source.id,
            "run_id": run.id,
            "updated": a_updated.id,
            "new": a_new.id,
            "unchanged": a_unchanged.id,
            "removed": a_removed.id,
        }


def _flatten(entries):
    out = []
    for e in entries:
        out.append(e)
        out.extend(_flatten(e["children"]))
    return out


async def test_browse_annotates_change_status(client):
    client, TestSession = client
    ids = await _seed_browse(TestSession)

    resp = await client.get(f"/api/sources/{ids['source_id']}/browse")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_run_id"] == str(ids["run_id"])

    nodes = _flatten(body["entries"])
    by_article = {n["article_id"]: n for n in nodes if n["article_id"]}

    assert by_article[str(ids["updated"])]["change_status"] == "updated"
    assert by_article[str(ids["updated"])]["version_count"] == 1
    assert by_article[str(ids["new"])]["change_status"] == "new"
    assert by_article[str(ids["unchanged"])]["change_status"] == "unchanged"

    # The section node carries no change status and nests its children.
    section = next(n for n in body["entries"] if not n["is_article"])
    assert section["change_status"] is None
    assert len(section["children"]) == 3


async def test_browse_lists_removed_pages(client):
    client, TestSession = client
    ids = await _seed_browse(TestSession)

    resp = await client.get(f"/api/sources/{ids['source_id']}/browse")
    body = resp.json()

    removed_ids = {r["article_id"] for r in body["removed"]}
    assert removed_ids == {str(ids["removed"])}
    # Removed pages are not present in the TOC tree.
    tree_article_ids = {
        n["article_id"] for n in _flatten(body["entries"]) if n["article_id"]
    }
    assert str(ids["removed"]) not in tree_article_ids


async def test_browse_unknown_source_404(client):
    client, _ = client
    resp = await client.get(f"/api/sources/{uuid.uuid4()}/browse")
    assert resp.status_code == 404


# ── Article provenance metadata ──

async def test_article_metadata_vendor_product_and_chapters(client):
    client, TestSession = client
    async with TestSession() as s:
        vendor = Vendor(name="Acme Corp")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="Acme Cloud", base_url="https://d.acme.com"
        )
        s.add(source)
        await s.flush()

        # 3-level TOC: Guide > Setup > Install (the article's own node).
        guide = TOCEntry(source_id=source.id, title="Guide", level=0, sort_order=0)
        s.add(guide)
        await s.flush()
        setup = TOCEntry(
            source_id=source.id, title="Setup", level=1, sort_order=1,
            parent_id=guide.id, is_article=False,
        )
        s.add(setup)
        await s.flush()
        install = TOCEntry(
            source_id=source.id, title="Install", level=2, sort_order=2,
            parent_id=setup.id,
        )
        s.add(install)
        await s.flush()

        article = Article(
            source_id=source.id, toc_entry_id=install.id, title="Install",
            source_url="https://d.acme.com/install",
            content_markdown="# Install\n\nSteps.", sort_order=0,
            estimated_tokens=5, content_size_bytes=20,
        )
        s.add(article)
        await s.commit()
        article_id = article.id

    resp = await client.get(f"/api/articles/{article_id}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["vendor"]["name"] == "Acme Corp"
    assert body["product"]["name"] == "Acme Cloud"
    assert body["parent_chapter"]["title"] == "Setup"
    assert body["top_level_chapter"]["title"] == "Guide"
    assert body["source_url"] == "https://d.acme.com/install"
    assert body["created_at"] and body["extracted_at"]


async def test_article_metadata_top_level_page_has_no_parent(client):
    """A page that is itself a top-level TOC entry has no parent chapter."""
    client, TestSession = client
    async with TestSession() as s:
        vendor = Vendor(name="Solo Vendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="Solo Product", base_url="https://d.solo.com"
        )
        s.add(source)
        await s.flush()
        top = TOCEntry(source_id=source.id, title="Announcements", level=0, sort_order=0)
        s.add(top)
        await s.flush()
        article = Article(
            source_id=source.id, toc_entry_id=top.id, title="Announcements",
            source_url="https://d.solo.com/news",
            content_markdown="news", sort_order=0,
            estimated_tokens=5, content_size_bytes=10,
        )
        s.add(article)
        await s.commit()
        article_id = article.id

    body = (await client.get(f"/api/articles/{article_id}")).json()
    assert body["parent_chapter"] is None
    assert body["top_level_chapter"]["title"] == "Announcements"


# ── Scrape-time metadata semantics (process_article_result) ──

async def test_extracted_at_tracks_last_scrape_created_at_is_first(client):
    """created_at stays first-seen; extracted_at advances on update and unchanged
    re-scrapes; last_updated_at stays NULL when the page exposes no timestamp."""
    client, TestSession = client
    async with TestSession() as s:
        vendor = Vendor(name="ScrapeVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="ScrapeSource", base_url="https://d.sc.com"
        )
        s.add(source)
        await s.flush()
        run = ExtractionRun(source_id=source.id, status=RunStatus.RUNNING)
        s.add(run)
        await s.commit()
        source_id, run_id = source.id, run.id

    url = "https://d.sc.com/page"

    async def fetch():
        async with TestSession() as s:
            row = (
                await s.execute(select(Article).where(Article.source_url == url))
            ).scalar_one()
            return row.created_at, row.extracted_at, row.last_updated_at, row.content_hash

    async with TestSession() as s:
        outcome = await firecrawl_service.process_article_result(
            db=s, source_id=source_id, run_id=run_id, url=url,
            markdown_content="version one", doc_html="", toc_entry_id=None,
            sort_order=0, title="Page", change_status=None,
        )
    assert outcome == "new"
    created1, extracted1, last_updated1, hash1 = await fetch()
    assert last_updated1 is None  # no <time> in (empty) html

    await asyncio.sleep(0.05)
    async with TestSession() as s:
        outcome = await firecrawl_service.process_article_result(
            db=s, source_id=source_id, run_id=run_id, url=url,
            markdown_content="version two changed", doc_html="", toc_entry_id=None,
            sort_order=0, title="Page", change_status=None,
        )
    assert outcome == "updated"
    created2, extracted2, last_updated2, hash2 = await fetch()
    assert created2 == created1            # first-seen unchanged
    assert extracted2 > extracted1         # last scrape advanced
    assert last_updated2 is None
    assert hash2 != hash1

    await asyncio.sleep(0.05)
    async with TestSession() as s:
        outcome = await firecrawl_service.process_article_result(
            db=s, source_id=source_id, run_id=run_id, url=url,
            markdown_content="version two changed", doc_html="", toc_entry_id=None,
            sort_order=0, title="Page", change_status=None,
        )
    assert outcome == "unchanged"
    created3, extracted3, _, hash3 = await fetch()
    assert created3 == created1            # still first-seen
    assert extracted3 > extracted2         # unchanged re-scrape still bumps scrape time
    assert hash3 == hash2


async def test_unchanged_paths_relink_rebuilt_toc(client):
    """Each run deletes+rebuilds TOC entries (new ids), nulling articles.toc_entry_id
    via SET NULL. The unchanged fast paths ('same' and hash-match) must re-link the
    article to the freshly-built toc entry — otherwise every unchanged article orphans
    on incremental runs and the browser renders nothing."""
    client, TestSession = client
    async with TestSession() as s:
        vendor = Vendor(name="RelinkVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="RelinkSrc", base_url="https://d.rl.com"
        )
        s.add(source)
        await s.flush()
        run = ExtractionRun(source_id=source.id, status=RunStatus.RUNNING)
        s.add(run)
        await s.flush()
        toc_a = TOCEntry(
            source_id=source.id, title="Page", url="https://d.rl.com/p",
            level=0, sort_order=0, is_article=True,
        )
        s.add(toc_a)
        await s.commit()
        source_id, run_id, toc_a_id = source.id, run.id, toc_a.id

    url = "https://d.rl.com/p"

    async def fetch():
        async with TestSession() as s:
            row = (
                await s.execute(select(Article).where(Article.source_url == url))
            ).scalar_one()
            return row.toc_entry_id, row.sort_order

    async def rebuild_toc(new_sort_order: int):
        """Drop the TOC (orphans the article via SET NULL) and create a new entry."""
        async with TestSession() as s:
            await s.execute(delete(TOCEntry).where(TOCEntry.source_id == source_id))
            toc = TOCEntry(
                source_id=source_id, title="Page", url=url,
                level=0, sort_order=new_sort_order, is_article=True,
            )
            s.add(toc)
            await s.commit()
            return toc.id

    # First extraction: article created and linked to toc_a.
    async with TestSession() as s:
        outcome = await firecrawl_service.process_article_result(
            db=s, source_id=source_id, run_id=run_id, url=url,
            markdown_content="stable body", doc_html="", toc_entry_id=toc_a_id,
            sort_order=0, title="Page", change_status="new",
        )
    assert outcome in ("new", "updated")
    assert (await fetch())[0] == toc_a_id

    # Rebuild → article orphaned → 'same' fast path must re-link + refresh sort_order.
    toc_b_id = await rebuild_toc(new_sort_order=7)
    assert (await fetch())[0] is None
    async with TestSession() as s:
        outcome = await firecrawl_service.process_article_result(
            db=s, source_id=source_id, run_id=run_id, url=url,
            markdown_content="stable body", doc_html="", toc_entry_id=toc_b_id,
            sort_order=7, title="Page", change_status="same",
        )
    assert outcome == "unchanged"
    assert await fetch() == (toc_b_id, 7)

    # Rebuild again → orphaned → hash-match path (change_status None) must also re-link.
    toc_c_id = await rebuild_toc(new_sort_order=3)
    assert (await fetch())[0] is None
    async with TestSession() as s:
        outcome = await firecrawl_service.process_article_result(
            db=s, source_id=source_id, run_id=run_id, url=url,
            markdown_content="stable body", doc_html="", toc_entry_id=toc_c_id,
            sort_order=3, title="Page", change_status=None,
        )
    assert outcome == "unchanged"
    assert await fetch() == (toc_c_id, 3)


async def test_reconcile_removals_stamps_clears_and_pins(client):
    """Newly orphaned articles get removed_at/removal_run_id; the timestamp is
    pinned on later runs; a re-added page is cleared; linked pages stay NULL."""
    client, TestSession = client
    async with TestSession() as s:
        vendor = Vendor(name="RemVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="RemSrc", base_url="https://rm.com"
        )
        s.add(source)
        await s.flush()
        # Both COMPLETED: the active-run-per-source unique index forbids two
        # simultaneously pending/running runs for one source.
        run1 = ExtractionRun(source_id=source.id, status=RunStatus.COMPLETED)
        run2 = ExtractionRun(source_id=source.id, status=RunStatus.COMPLETED)
        s.add_all([run1, run2])
        await s.flush()
        toc = TOCEntry(
            source_id=source.id, title="Kept", url="https://rm.com/k",
            level=0, sort_order=0, is_article=True,
        )
        s.add(toc)
        await s.flush()

        def mk(title, url, toc_id):
            return Article(
                source_id=source.id, toc_entry_id=toc_id, title=title,
                source_url=url, content_markdown="x", content_hash="h",
                sort_order=0, estimated_tokens=1, content_size_bytes=1,
            )

        kept = mk("Kept", "https://rm.com/k", toc.id)       # linked → never removed
        gone = mk("Gone", "https://rm.com/g", None)          # orphaned → removed
        s.add_all([kept, gone])
        await s.commit()
        source_id, run1_id, run2_id = source.id, run1.id, run2.id
        kept_id, gone_id, toc_id = kept.id, gone.id, toc.id

    async def fetch(aid):
        async with TestSession() as s:
            a = (await s.execute(select(Article).where(Article.id == aid))).scalar_one()
            return a.removed_at, a.removal_run_id, a.toc_entry_id

    # Run 1 reconcile: 'gone' is stamped, 'kept' untouched.
    async with TestSession() as s:
        await firecrawl_service._reconcile_removals(s, source_id, run1_id)
    g_removed1, g_run1, _ = await fetch(gone_id)
    k_removed, _, _ = await fetch(kept_id)
    assert g_removed1 is not None and g_run1 == run1_id
    assert k_removed is None

    # Run 2 reconcile (still orphaned): timestamp + run pinned to first detection.
    async with TestSession() as s:
        await firecrawl_service._reconcile_removals(s, source_id, run2_id)
    g_removed2, g_run2, _ = await fetch(gone_id)
    assert g_removed2 == g_removed1 and g_run2 == run1_id

    # 'gone' is re-added (re-linked to a toc entry), then reconcile clears it.
    async with TestSession() as s:
        a = (await s.execute(select(Article).where(Article.id == gone_id))).scalar_one()
        a.toc_entry_id = toc_id
        await s.commit()
    async with TestSession() as s:
        await firecrawl_service._reconcile_removals(s, source_id, run2_id)
    g_removed3, g_run3, _ = await fetch(gone_id)
    assert g_removed3 is None and g_run3 is None


async def test_changelog_collapses_baseline_and_merges_events(client):
    """The baseline (first) run collapses to one 'initial' summary entry; later
    runs emit per-page added/changed/removed events, newest-first."""
    client, TestSession = client
    T_OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)   # baseline run
    T_MID = datetime(2026, 3, 1, tzinfo=timezone.utc)   # A changed
    T_NEW = datetime(2026, 6, 1, tzinfo=timezone.utc)   # incremental run
    async with TestSession() as s:
        vendor = Vendor(name="ClVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="ClSrc", base_url="https://cl.com"
        )
        s.add(source)
        await s.flush()
        run1 = ExtractionRun(source_id=source.id, status=RunStatus.COMPLETED, started_at=T_OLD)
        run2 = ExtractionRun(source_id=source.id, status=RunStatus.COMPLETED, started_at=T_NEW)
        s.add_all([run1, run2])
        await s.flush()
        # Baseline articles A and B (created in run1). A is later changed; B removed.
        a = Article(
            source_id=source.id, toc_entry_id=None, created_run_id=run1.id, title="Page A",
            source_url="https://cl.com/a", content_markdown="now", content_hash="h2",
            sort_order=0, estimated_tokens=1, content_size_bytes=1, created_at=T_OLD,
        )
        b = Article(
            source_id=source.id, toc_entry_id=None, created_run_id=run1.id, title="Page B",
            source_url="https://cl.com/b", content_markdown="x", content_hash="h",
            sort_order=0, estimated_tokens=1, content_size_bytes=1,
            created_at=T_OLD, removed_at=T_NEW, removal_run_id=run2.id,
        )
        # New article C, added in the incremental run2 → per-page 'added' event.
        c = Article(
            source_id=source.id, toc_entry_id=None, created_run_id=run2.id, title="New C",
            source_url="https://cl.com/c", content_markdown="c", content_hash="hc",
            sort_order=0, estimated_tokens=1, content_size_bytes=1, created_at=T_NEW,
        )
        s.add_all([a, b, c])
        await s.flush()
        s.add(ArticleVersion(
            article_id=a.id, extraction_run_id=run2.id, content_markdown="old",
            content_hash="h1", diff_text="@@ -1 +1 @@", extracted_at=T_MID,
        ))
        await s.commit()
        source_id = source.id

    r = await client.get(f"/api/sources/{source_id}/changelog")
    assert r.status_code == 200
    data = r.json()
    # 1 initial (baseline collapses A+B) + 1 added (C) + 1 changed (A) + 1 removed (B).
    assert data["total"] == 4
    by_type = {e["change_type"]: e for e in data["entries"]}
    assert set(by_type) == {"initial", "added", "changed", "removed"}
    assert by_type["initial"]["article_id"] is None
    assert "2 articles" in by_type["initial"]["title"]
    assert by_type["added"]["title"] == "New C"
    assert by_type["changed"]["version_id"] is not None and by_type["changed"]["has_diff"] is True
    assert by_type["removed"]["version_id"] is None
    # The initial summary is the oldest event (baseline run time).
    assert data["entries"][-1]["change_type"] == "initial"
