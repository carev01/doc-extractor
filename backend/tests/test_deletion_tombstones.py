"""Deletion tombstones + append-only content_changes (async httpx client)."""
import os
import sys
import uuid

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
from app.models.content_change import ContentChange, ChangeType

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


async def _seed_article(factory):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        art = Article(source_id=src.id, title="A", source_url="https://s/a",
                      topic_key="https://s/a", content_markdown="# A", content_hash="h")
        s.add(art); await s.flush()
        cc = ContentChange(article_id=art.id, source_id=src.id, run_id=None,
                           change_type=ChangeType.ADDED.value, content_hash="h",
                           topic_key="https://s/a")
        s.add(cc); await s.commit()
        return src.id, art.id, cc.id


async def test_hard_deleting_article_preserves_outbox_ids(ctx):
    """content_changes is append-only: deleting the article must NOT null its
    article_id on the historical outbox row."""
    _c, factory = ctx
    src_id, art_id, cc_id = await _seed_article(factory)
    async with factory() as s:
        art = (await s.execute(select(Article).where(Article.id == art_id))).scalar_one()
        await s.delete(art)
        await s.commit()
    async with factory() as s:
        cc = (await s.execute(select(ContentChange).where(ContentChange.id == cc_id))).scalar_one()
        assert cc.article_id == art_id      # was nulled by the SET NULL FK before this task
        assert cc.source_id == src_id


from app.models import ExtractionRun
from app.models.extraction_run import RunStatus


async def _seed_two_articles(factory, status=None):
    # NOTE: unique per-call suffix (not in the task brief's verbatim snippet) —
    # Vendor.name is unique, and test_delete_product_and_vendor_emit_tombstones
    # calls this helper twice in one test (product delete doesn't remove the
    # vendor), so a fixed "V" name collides on the second call. See task-3-report.md.
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        v = Vendor(name=f"V-{suffix}"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        a1 = Article(source_id=src.id, title="1", source_url="https://s/1",
                     topic_key="https://s/1", content_markdown="#1", content_hash="h1")
        a2 = Article(source_id=src.id, title="2", source_url="https://s/2",
                     topic_key="https://s/2", content_markdown="#2", content_hash="h2")
        s.add_all([a1, a2]); await s.commit()
        return v.id, p.id, src.id, {a1.id, a2.id}


async def test_delete_source_emits_tombstones_with_intact_ids(ctx):
    c, factory = ctx
    _v, _p, src_id, art_ids = await _seed_two_articles(factory)
    r = await c.delete(f"/api/sources/{src_id}")
    assert r.status_code == 204
    async with factory() as s:
        rows = (await s.execute(
            select(ContentChange).where(ContentChange.change_type == ChangeType.REMOVED.value)
        )).scalars().all()
        assert {row.article_id for row in rows} == art_ids       # ids survived the hard delete
        assert all(row.run_id is None for row in rows)
        # articles are actually gone
        arts = (await s.execute(select(Article).where(Article.source_id == src_id))).scalars().all()
        assert arts == []


async def test_delete_product_and_vendor_emit_tombstones(ctx):
    c, factory = ctx
    _v, p_id, _src, art_ids = await _seed_two_articles(factory)
    assert (await c.delete(f"/api/products/{p_id}")).status_code == 204
    async with factory() as s:
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.change_type == ChangeType.REMOVED.value))).scalars().all()
        assert {row.article_id for row in rows} == art_ids

    v_id, _p, _src, art_ids2 = await _seed_two_articles(factory)
    assert (await c.delete(f"/api/vendors/{v_id}")).status_code == 204
    async with factory() as s:
        rows = (await s.execute(select(ContentChange).where(
            ContentChange.change_type == ChangeType.REMOVED.value))).scalars().all()
        assert art_ids2.issubset({row.article_id for row in rows})


async def test_source_deletions_serialize_via_advisory_lock(ctx):
    """Concurrent out-of-band deletions must serialize: their removals carry
    run_id=None with no run_start floor, so if two interleave BIGSERIAL ids with
    an inverted commit order (and no active run sets a ceiling) the feed would
    skip the lower id forever — a lost tombstone. record_source_deletions takes a
    transaction-scoped advisory lock so a second deletion cannot proceed until
    the first commits."""
    from sqlalchemy import text
    from app.services import change_log
    _c, factory = ctx
    _v, _p, src_id, _ids = await _seed_two_articles(factory)

    async with factory() as s1:
        await change_log.record_source_deletions(s1, source_ids=[src_id])  # holds the lock
        async with factory() as s2:
            got = (await s2.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": change_log._DELETION_LOCK_KEY},
            )).scalar()
            assert got is False        # a concurrent deletion is blocked
        await s1.commit()


async def test_deletion_removals_respect_safe_ceiling(ctx):
    """A run_id=NULL removal committed above an active run's run_start floor is
    withheld until that run finishes, then served."""
    from app.services import change_log
    c, factory = ctx
    # An unrelated active run with a run_start sentinel (a low floor).
    async with factory() as s:
        v = Vendor(name="RV"); s.add(v); await s.flush()
        p = Product(name="RP", vendor_id=v.id); s.add(p); await s.flush()
        run_src = DocumentationSource(name="RS", base_url="https://r", product_id=p.id, source_type="web")
        s.add(run_src); await s.flush()
        run = ExtractionRun(source_id=run_src.id, status=RunStatus.RUNNING, kind="extract")
        s.add(run); await s.flush()
        await change_log.record_run_start(s, source_id=run_src.id, run_id=run.id)
        await s.commit()
        run_id = run.id

    _v, _p, src_id, _ids = await _seed_two_articles(factory)
    assert (await c.delete(f"/api/sources/{src_id}")).status_code == 204

    # Bootstrap gives us the current cursor; the removals sit above the active
    # run's floor, so an incremental pull withholds them.
    boot = (await c.get("/api/articles/delta")).text.strip().splitlines()
    import json
    cursor = json.loads(boot[-1])["next_since"]
    removed = [json.loads(l) for l in (await c.get(f"/api/articles/delta?since={cursor}")).text.splitlines()
               if l and json.loads(l).get("change_type") == "removed"]
    assert removed == []                       # withheld while the run is active

    async with factory() as s:
        run = (await s.execute(select(ExtractionRun).where(ExtractionRun.id == run_id))).scalar_one()
        run.status = RunStatus.COMPLETED
        await s.commit()

    removed_after = [json.loads(l) for l in (await c.get(f"/api/articles/delta?since={cursor}")).text.splitlines()
                     if l and json.loads(l).get("change_type") == "removed"]
    assert len(removed_after) >= 2             # now served
