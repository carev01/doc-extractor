"""One-off split of the NetBackup source into per-publication sources.

Cohesity's sidebar now lists only the current publication, so the single
NetBackup source (33 publications, ~10k live articles) is split in place: each
publication becomes its own source, live articles move with their id intact,
prior-release copies orphaned by the 11.2 -> 11.2.0.1 bump fold into the live
article's history, and pages from retired publications are dropped. See
docs/superpowers/specs/2026-10-27-netbackup-source-split-design.md.

The fixture mirrors the real corpus' shapes: literal build ids in most keys,
the Release Notes key already in {rev} form, removed rows whose source_url still
carries the old "11.2" segment, and a TOC whose root heading spans publications.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import (
    Article, ArticleVersion, DocumentationSource, ExtractionRun, Product, Vendor,
)
from app.models.content_change import ContentChange
from app.models.toc import TOCEntry
from app.ops.netbackup_split import Guide, SplitAborted, apply_split, build_split_plan, run
from app.schemas.delta import encode_delta_cursor
from app.services.versioning import derive_topic_key

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

NB = "https://docs.cohesity.com/docs/netbackup"
RN, SC = "103228346", "44037985"  # Release Notes, Status Codes
GUIDES = (Guide(RN, "173151032", "Release Notes"), Guide(SC, "173488551", "Status Codes"))
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _article(src, title, url, key, *, removed=False, at=T0, h=None):
    return Article(
        source_id=src, title=title, source_url=url, topic_key=key,
        content_markdown=f"body of {title}", content_hash=h or f"h-{title}",
        extracted_at=at, removed_at=at + timedelta(days=1) if removed else None,
        content_changed_at=at,
    )


@pytest_asyncio.fixture
async def corpus(factory):
    async with factory() as s:
        v = Vendor(name="Cohesity"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="NetBackup", version="11.2.0.1"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="Cohesity NetBackup", platform="collapsible_sidebar",
            base_url=f"{NB}/11.2.0.1/{RN}-173151032-0/v95650213-173151032",
            url_template=f"{NB}/{{version}}/{RN}-{{rev}}-0/v95650213-{{rev}}",
            profile_config={"k": "v"},
        )
        s.add(src); await s.flush()

        # TOC: ROOT (spans both guides) -> [H -> [L1 page], RN page]
        root = TOCEntry(source_id=src.id, title="ROOT", url=None, level=0, is_article=False)
        s.add(root); await s.flush()
        h = TOCEntry(source_id=src.id, title="H", url=None, level=1, parent_id=root.id, is_article=False)
        rn_page = TOCEntry(source_id=src.id, title="RN page", level=1, parent_id=root.id,
                           url=f"{NB}/11.2.0.1/{RN}-173151032-0/v95650213-173151032")
        s.add_all([h, rn_page]); await s.flush()
        l1_page = TOCEntry(source_id=src.id, title="L1 page", level=2, parent_id=h.id,
                           url=f"{NB}/11.2.0.1/{SC}-173488551-0/v15098603-173488551")
        s.add(l1_page); await s.flush()

        l1 = _article(src.id, "L1", f"{NB}/11.2.0.1/{SC}-173488551-0/v15098603-173488551",
                      f"{NB}/{{version}}/{SC}-173488551-0/v15098603-173488551", at=T0 + timedelta(days=60))
        l1.toc_entry_id = l1_page.id
        l2 = _article(src.id, "L2", f"{NB}/11.2.0.1/{RN}-173151032-0/v95650213-173151032",
                      f"{NB}/{{version}}/{RN}-{{rev}}-0/v95650213-{{rev}}", at=T0 + timedelta(days=60))
        l2.toc_entry_id = rn_page.id
        # Prior-release copies of L1; source_url carries the old 11.2 segment.
        t1a = _article(src.id, "T1a", f"{NB}/11.2/{SC}-170000001-0/v15098603-170000001",
                       f"{NB}/{{version}}/{SC}-170000001-0/v15098603-170000001", removed=True, at=T0)
        t1b = _article(src.id, "T1b", f"{NB}/11.2/{SC}-170000002-0/v15098603-170000002",
                       f"{NB}/{{version}}/{SC}-170000002-0/v15098603-170000002", removed=True,
                       at=T0 + timedelta(days=30))
        # A page removed from a live guide, removed in both eras.
        r1old = _article(src.id, "R1old", f"{NB}/11.2/{SC}-170000001-0/v15000000-170000001",
                         f"{NB}/{{version}}/{SC}-170000001-0/v15000000-170000001", removed=True, at=T0)
        r1new = _article(src.id, "R1new", f"{NB}/11.2/{SC}-170000002-0/v15000000-170000002",
                         f"{NB}/{{version}}/{SC}-170000002-0/v15000000-170000002", removed=True,
                         at=T0 + timedelta(days=30))
        # A page from a publication that no longer exists.
        x1 = _article(src.id, "X1", f"{NB}/11.2/99999999-170000001-0/v1-170000001",
                      f"{NB}/{{version}}/99999999-170000001-0/v1-170000001", removed=True, at=T0)
        s.add_all([l1, l2, t1a, t1b, r1old, r1new, x1]); await s.flush()
        s.add(ArticleVersion(article_id=t1a.id, content_markdown="T1a v0", content_hash="h-T1a-v0",
                             extracted_at=T0 - timedelta(days=10)))
        await s.commit()
        return SimpleNamespace(
            factory=factory, source_id=src.id, product_id=p.id, guides=GUIDES,
            L1=l1.id, L2=l2.id, T1a=t1a.id, T1b=t1b.id, R1old=r1old.id, R1new=r1new.id,
            X1=x1.id, ROOT=root.id, H=h.id, RN_PAGE=rn_page.id, L1_PAGE=l1_page.id,
        )


async def _plan(c):
    async with c.factory() as s:
        return await build_split_plan(s, c.source_id, c.guides)


async def test_plan_classifies_every_row(corpus):
    plan = await _plan(corpus)
    assert plan.live[corpus.L1] == (SC, f"{NB}/{{version}}/{SC}-{{rev}}-0/v15098603-{{rev}}")
    assert plan.live[corpus.L2] == (RN, f"{NB}/{{version}}/{RN}-{{rev}}-0/v95650213-{{rev}}")
    assert plan.history[corpus.L1] == [corpus.T1a, corpus.T1b]
    assert plan.removed_moves[corpus.R1new][0] == SC
    assert plan.history[corpus.R1new] == [corpus.R1old]
    assert plan.retired == [corpus.X1]
    assert plan.toc[corpus.H] == SC and plan.toc[corpus.L1_PAGE] == SC
    assert plan.toc[corpus.RN_PAGE] == RN and plan.toc[corpus.ROOT] is None
    assert plan.sources[SC].url_template == f"{NB}/{{version}}/{SC}-{{rev}}-0"
    assert plan.sources[SC].base_url == f"{NB}/11.2.0.1/{SC}-173488551-0"
    assert plan.sources[SC].name == "Status Codes"
    assert plan.counts() == {"live": 2, "folded": 3, "removed_moves": 1, "retired": 1,
                             "toc_moved": 3, "toc_left": 1}


async def test_new_key_equals_what_a_run_would_derive(corpus):
    plan = await _plan(corpus)
    async with corpus.factory() as s:
        l1 = await s.get(Article, corpus.L1)
    run_key = derive_topic_key(l1.source_url, plan.sources[SC].url_template, "11.2.0.1")
    assert plan.live[corpus.L1][1] == run_key


async def test_live_article_outside_the_guides_aborts(corpus):
    async with corpus.factory() as s:
        s.add(_article(corpus.source_id, "Z", f"{NB}/11.2.0.1/88888888-1-0/v2-1",
                       f"{NB}/{{version}}/88888888-1-0/v2-1"))
        await s.commit()
    with pytest.raises(SplitAborted, match="88888888"):
        await _plan(corpus)


async def test_version_mismatch_aborts(corpus):
    async with corpus.factory() as s:
        (await s.get(Product, corpus.product_id)).version = "11.3"
        await s.commit()
    with pytest.raises(SplitAborted, match="version"):
        await _plan(corpus)


# ── apply ────────────────────────────────────────────────────────────────────

async def _apply(c):
    async with c.factory() as s:
        plan = await build_split_plan(s, c.source_id, c.guides)
        result = await apply_split(s, c.source_id, plan)
        await s.commit()
        return result


async def _versions(c, article_id):
    async with c.factory() as s:
        return (await s.execute(
            select(ArticleVersion.content_markdown).where(ArticleVersion.article_id == article_id)
            .order_by(ArticleVersion.extracted_at)
        )).scalars().all()


async def test_apply_moves_folds_and_drops(corpus):
    result = await _apply(corpus)
    sc_src, rn_src = result.source_ids[SC], result.source_ids[RN]
    async with corpus.factory() as s:
        l1 = await s.get(Article, corpus.L1)
        assert l1.source_id == sc_src
        assert l1.topic_key == f"{NB}/{{version}}/{SC}-{{rev}}-0/v15098603-{{rev}}"
        assert (await s.get(Article, corpus.L2)).source_id == rn_src
        for gone in (corpus.T1a, corpus.T1b, corpus.R1old, corpus.X1):
            assert await s.get(Article, gone) is None
        r1 = await s.get(Article, corpus.R1new)
        assert r1.source_id == sc_src and r1.removed_at is not None
        new = await s.get(DocumentationSource, sc_src)
        assert (new.name, new.base_url, new.platform, new.profile_config) == (
            "Status Codes", f"{NB}/11.2.0.1/{SC}-173488551-0", "collapsible_sidebar", {"k": "v"})
        assert new.url_template == f"{NB}/{{version}}/{SC}-{{rev}}-0"
        left = (await s.execute(select(func.count()).select_from(Article).where(
            Article.source_id == corpus.source_id, Article.removed_at.is_(None)))).scalar_one()
        assert left == 0
        assert (await s.get(TOCEntry, corpus.ROOT)).source_id == corpus.source_id
        h = await s.get(TOCEntry, corpus.H)
        assert h.source_id == sc_src and h.parent_id is None  # parent ROOT stayed behind
    assert await _versions(corpus, corpus.L1) == ["T1a v0", "body of T1a", "body of T1b"]
    assert await _versions(corpus, corpus.R1new) == ["body of R1old"]
    assert result.moved == 2 and result.outbox == 2


async def test_apply_does_not_touch_content_changed_at(corpus):
    await _apply(corpus)
    async with corpus.factory() as s:
        assert (await s.get(Article, corpus.L1)).content_changed_at == T0 + timedelta(days=60)


async def test_identical_consecutive_hash_is_not_duplicated(corpus):
    async with corpus.factory() as s:
        (await s.get(Article, corpus.T1b)).content_hash = "h-L1"
        await s.commit()
    await _apply(corpus)
    assert await _versions(corpus, corpus.L1) == ["T1a v0", "body of T1a"]


async def test_apply_refuses_when_a_run_is_active(corpus):
    async with corpus.factory() as s:
        s.add(ExtractionRun(source_id=corpus.source_id, status="running"))
        await s.commit()
    with pytest.raises(SplitAborted, match="active"):
        await _apply(corpus)
    async with corpus.factory() as s:
        n = (await s.execute(select(func.count()).select_from(DocumentationSource))).scalar_one()
        assert n == 1


async def test_second_apply_aborts(corpus):
    await _apply(corpus)
    with pytest.raises(SplitAborted):
        await _apply(corpus)


async def test_delta_feed_serves_the_move(corpus):
    async def override_get_db():
        async with corpus.factory() as session:
            yield session
    result = await _apply(corpus)
    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    finally:
        app.dependency_overrides.clear()
    records = [r for r in map(json.loads, resp.text.splitlines()) if "control" not in r]
    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {str(corpus.L1), str(corpus.L2)}
    l1 = by_id[str(corpus.L1)]
    assert l1["change_type"] == "updated" and l1["source_id"] == str(result.source_ids[SC])
    assert l1["topic_key"] == f"{NB}/{{version}}/{SC}-{{rev}}-0/v15098603-{{rev}}"
    assert (l1["parent_chapter"], l1["top_level_chapter"]) == ("H", "H")
    assert l1["content_changed_at"].startswith("2026-08-30")
    assert by_id[str(corpus.L2)]["top_level_chapter"] == "RN page"


# ── CLI ──────────────────────────────────────────────────────────────────────

async def test_dry_run_writes_nothing(corpus):
    result = await run(TEST_DATABASE_URL, corpus.source_id, apply=False, guides=corpus.guides)
    assert result.moved == 2
    async with corpus.factory() as s:
        assert (await s.execute(select(func.count()).select_from(DocumentationSource))).scalar_one() == 1
        assert (await s.get(Article, corpus.L1)).source_id == corpus.source_id
        assert await s.get(Article, corpus.T1a) is not None


async def test_apply_run_commits(corpus):
    result = await run(TEST_DATABASE_URL, corpus.source_id, apply=True, guides=corpus.guides)
    async with corpus.factory() as s:
        assert (await s.get(Article, corpus.L1)).source_id == result.source_ids[SC]


async def test_apply_refuses_without_the_child_fk_indexes(corpus):
    # Without them every cascaded article delete seq-scans article_versions; on a
    # production copy the split's bulk delete ran past ten minutes.
    async with corpus.factory() as s:
        await s.execute(text("DROP INDEX ix_article_versions_article_id"))
        await s.commit()
    with pytest.raises(SplitAborted, match="ix_article_versions_article_id"):
        await _apply(corpus)
