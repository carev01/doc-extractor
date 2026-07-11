"""Integration tests for GET /api/articles/delta (async httpx client)."""
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
from app.schemas.delta import encode_delta_cursor

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


def _parse(text_body):
    """Split an NDJSON body into (records, control)."""
    lines = [json.loads(l) for l in text_body.splitlines() if l.strip()]
    control = lines[-1]
    assert control.get("control") == "cursor"
    return lines[:-1], control


async def _seed_source(factory, *, run_status=RunStatus.COMPLETED):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://x", product_id=p.id)
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status=run_status); s.add(run); await s.flush()
        await s.commit()
        return v.id, src.id, run.id


async def _add_article_change(factory, src_id, run_id, *, url, title, change_type="added"):
    async with factory() as s:
        art = Article(
            source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
            title=title, source_url=url, topic_key=url,
            content_markdown=f"# {title}", content_hash=f"h-{title}",
        )
        s.add(art); await s.flush()
        s.add(ContentChange(
            article_id=art.id, source_id=src_id, run_id=run_id,
            change_type=change_type, content_hash=art.content_hash, topic_key=url,
        ))
        await s.commit()
        return art.id


async def test_bootstrap_streams_all_articles(ctx):
    c, factory = ctx
    _, src_id, run_id = await _seed_source(factory)
    await _add_article_change(factory, src_id, run_id, url="https://x/a", title="A")
    await _add_article_change(factory, src_id, run_id, url="https://x/b", title="B")

    resp = await c.get("/api/articles/delta")  # no since → bootstrap
    assert resp.status_code == 200
    records, control = _parse(resp.text)
    assert {r["title"] for r in records} == {"A", "B"}
    assert all(r["change_type"] == "added" for r in records)
    assert all(r["seq"] is None for r in records)
    assert control["next_since"]  # a cursor to continue from


async def test_delta_since_returns_only_newer(ctx):
    c, factory = ctx
    _, src_id, run_id = await _seed_source(factory)
    await _add_article_change(factory, src_id, run_id, url="https://x/a", title="A")
    # Take a watermark, then add B.
    first = await c.get("/api/articles/delta")
    _, control = _parse(first.text)
    cursor = control["next_since"]
    await _add_article_change(factory, src_id, run_id, url="https://x/b", title="B")

    resp = await c.get(f"/api/articles/delta?since={cursor}")
    records, control2 = _parse(resp.text)
    titles = [r["title"] for r in records]
    assert titles == ["B"]
    assert records[0]["seq"] is not None


async def test_removed_emits_tombstone(ctx):
    c, factory = ctx
    _, src_id, run_id = await _seed_source(factory)
    art_id = await _add_article_change(factory, src_id, run_id, url="https://x/a", title="A")
    async with factory() as s:
        s.add(ContentChange(
            article_id=art_id, source_id=src_id, run_id=run_id,
            change_type="removed", topic_key="https://x/a",
        ))
        await s.commit()

    resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    records, _ = _parse(resp.text)
    removed = [r for r in records if r["change_type"] == "removed"]
    assert len(removed) == 1
    assert removed[0]["id"] == str(art_id)
    assert removed[0]["topic_key"] == "https://x/a"


async def test_safe_ceiling_withholds_active_run_rows(ctx):
    c, factory = ctx
    # Active (RUNNING) run whose change has a LOW id, plus a COMPLETED run whose
    # change has a HIGHER id. The completed row must be withheld until the running
    # run finishes, because its id sits above the running run's floor.
    _, src_id, running_run = await _seed_source(factory, run_status=RunStatus.RUNNING)
    await _add_article_change(factory, src_id, running_run, url="https://x/live", title="Live")
    async with factory() as s:
        done = ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED)
        s.add(done); await s.flush()
        done_id = done.id
        await s.commit()
    await _add_article_change(factory, src_id, done_id, url="https://x/done", title="Done")

    resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    records, control = _parse(resp.text)
    # Nothing served: the running run's low id is the ceiling; the completed row
    # sits above it and is withheld.
    assert records == []
    assert control["count"] == 0


async def test_invalid_cursor_422(ctx):
    c, factory = ctx
    resp = await c.get("/api/articles/delta?since=not-a-cursor!!")
    assert resp.status_code == 422


async def test_run_start_floor_withholds_higher_rows_of_other_runs(ctx):
    # A run_start sentinel for an ACTIVE run establishes a committed floor. A
    # COMPLETED run's change with a HIGHER id must be withheld until the active
    # run finishes — this is the mechanism that closes the flush→commit window.
    c, factory = ctx
    _, src_id, active_run = await _seed_source(factory, run_status=RunStatus.RUNNING)
    async with factory() as s:
        s.add(ContentChange(article_id=None, source_id=src_id, run_id=active_run,
                            change_type="run_start", topic_key=None))
        await s.commit()
    # Completed run whose article change has a higher id than the sentinel.
    async with factory() as s:
        done = ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED)
        s.add(done); await s.flush()
        done_id = done.id
        await s.commit()
    await _add_article_change(factory, src_id, done_id, url="https://x/done", title="Done")

    r1 = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    recs1, ctrl1 = _parse(r1.text)
    assert recs1 == [] and ctrl1["count"] == 0  # withheld: sentinel floor blocks higher ids

    # Complete the active run → floor lifts → the completed row is served, and the
    # run_start sentinel is skipped (not emitted as a record).
    async with factory() as s:
        run = await s.get(ExtractionRun, active_run)
        run.status = RunStatus.COMPLETED
        await s.commit()
    r2 = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    recs2, _ = _parse(r2.text)
    titles = [r.get("title") for r in recs2]
    assert titles == ["Done"]  # exactly the real change; sentinel skipped, no gap


async def test_content_record_run_id_is_change_run_not_article_run(ctx):
    # The delta content record's run_id must be the CHANGE's run, not the article's
    # latest extraction_run_id (they diverge if a later run touched the article).
    c, factory = ctx
    _, src_id, run_a = await _seed_source(factory)
    async with factory() as s:
        run_b = ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED)
        s.add(run_b); await s.flush()
        run_b_id = run_b.id
        art = Article(
            source_id=src_id, extraction_run_id=run_b_id, created_run_id=run_a,
            title="A", source_url="https://x/a", topic_key="https://x/a",
            content_markdown="# A", content_hash="h",
        )
        s.add(art); await s.flush()
        # The change belongs to run_a, but the article's latest run is run_b.
        s.add(ContentChange(article_id=art.id, source_id=src_id, run_id=run_a,
                            change_type="updated", content_hash="h", topic_key="https://x/a"))
        await s.commit()

    resp = await c.get(f"/api/articles/delta?since={encode_delta_cursor(0)}")
    recs, _ = _parse(resp.text)
    assert len(recs) == 1
    assert recs[0]["run_id"] == str(run_a)      # the change's run
    assert recs[0]["run_id"] != str(run_b_id)   # not the article's latest run
