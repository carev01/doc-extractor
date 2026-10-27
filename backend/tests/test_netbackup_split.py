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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Article, ArticleVersion, DocumentationSource, Product, Vendor
from app.models.toc import TOCEntry
from app.ops.netbackup_split import Guide, SplitAborted, build_split_plan
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
