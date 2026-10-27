"""One-off: soft-remove the stale literal-key twins that _reconcile_removals'
URL relink kept alive (CommCell Console 14,511, Arcserve RHA 356 on 2026-10-27).

A stale twin is a live article whose topic_key is a literal URL while a live
article in the same source, at the same source_url, carries the templated
({version}) key every run since has matched. The templated row is the real one;
the twin was created by a bad run and never updated again.
"""
import os
import sys
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Article, DocumentationSource, ExtractionRun, Product, Vendor
from app.models.content_change import ContentChange
from app.ops.dedupe_stale_twins import DedupeAborted, dedupe

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio
T = datetime(2026, 7, 1, tzinfo=timezone.utc)


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


def _a(src, url, key, removed=False):
    return Article(source_id=src, title=key, source_url=url, topic_key=key, content_markdown="x",
                   content_hash="h", content_changed_at=T, removed_at=T if removed else None)


@pytest_asyncio.fixture
async def corpus(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P", version="11.46"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="S", base_url="http://d/11.46/i",
                                  url_template="http://d/{version}/i")
        other = DocumentationSource(product_id=p.id, name="O", base_url="http://e/i")
        s.add_all([src, other]); await s.flush()
        real = _a(src.id, "http://d/11.46/a", "http://d/{version}/a")
        twin = _a(src.id, "http://d/11.46/a", "http://d/11.46/a")
        solo = _a(src.id, "http://d/11.46/b", "http://d/11.46/b")          # literal but no twin: keep
        gone = _a(src.id, "http://d/11.46/a", "http://d/11.44/a", removed=True)  # already removed
        pdf_a = _a(other.id, "http://e/doc#page=6", "k1")                   # other source: untouched
        pdf_b = _a(other.id, "http://e/doc#page=6", "k2")
        s.add_all([real, twin, solo, gone, pdf_a, pdf_b])
        await s.commit()
        return dict(factory=factory, src=src.id, other=other.id, real=real.id, twin=twin.id,
                    solo=solo.id, gone=gone.id)


async def test_removes_only_the_stale_twin_with_a_tombstone(corpus):
    f = corpus["factory"]
    async with f() as s:
        n = await dedupe(s, [corpus["src"], corpus["other"]])
        await s.commit()
    assert n == 1
    async with f() as s:
        twin = await s.get(Article, corpus["twin"])
        assert twin.removed_at is not None and twin.toc_entry_id is None
        assert twin.content_changed_at == T
        for keep in ("real", "solo"):
            assert (await s.get(Article, corpus[keep])).removed_at is None
        live_other = (await s.execute(select(func.count()).select_from(Article).where(
            Article.source_id == corpus["other"], Article.removed_at.is_(None)))).scalar_one()
        assert live_other == 2
        rows = (await s.execute(select(ContentChange))).scalars().all()
        assert [(r.article_id, r.change_type, r.source_id, r.run_id) for r in rows] == [
            (corpus["twin"], "removed", corpus["src"], None)]


async def test_second_pass_is_a_no_op(corpus):
    f = corpus["factory"]
    for expected in (1, 0):
        async with f() as s:
            assert await dedupe(s, [corpus["src"]]) == expected
            await s.commit()


async def test_refuses_during_an_active_run(corpus):
    f = corpus["factory"]
    async with f() as s:
        s.add(ExtractionRun(source_id=corpus["src"], status="running"))
        await s.commit()
    async with f() as s:
        with pytest.raises(DedupeAborted, match="active"):
            await dedupe(s, [corpus["src"]])
