"""Unit tests for the content_changes write helpers (sync psycopg2 session)."""
import asyncio
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    Vendor, Product, DocumentationSource, Article, ExtractionRun,
)
from app.models.content_change import ContentChange
from app.services import change_log

TEST_DATABASE_URL_SYNC = settings.database_url_sync.rsplit("/", 1)[0] + "/docextractor_test"
sync_engine = create_engine(TEST_DATABASE_URL_SYNC, echo=False)
SyncSession = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest.fixture(scope="function")
def db_session():
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    session = SyncSession()
    yield session
    session.rollback()
    session.close()


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory
    await engine.dispose()


def _seed(session):
    vendor = Vendor(name="V")
    session.add(vendor); session.flush()
    product = Product(name="P", vendor_id=vendor.id)
    session.add(product); session.flush()
    source = DocumentationSource(name="S", base_url="https://x", product_id=product.id)
    session.add(source); session.flush()
    run = ExtractionRun(source_id=source.id)
    session.add(run); session.flush()
    return source, run


def test_record_change_writes_row(db_session):
    source, run = _seed(db_session)
    article = Article(
        source_id=source.id, extraction_run_id=run.id, created_run_id=run.id,
        title="T", source_url="https://x/a", topic_key="https://x/a",
        content_markdown="# T", content_hash="hash-abc",
    )
    db_session.add(article); db_session.flush()

    # record_change is a coroutine but performs no awaited I/O — it only calls
    # db.add — so asyncio.run drives it to completion against a sync Session.
    # (asyncio.run, not get_event_loop().run_until_complete, to avoid the 3.12
    # "no current event loop" DeprecationWarning — test output must stay pristine.)
    asyncio.run(
        change_log.record_change(db_session, article=article, change_type="added", run_id=run.id)
    )
    db_session.commit()

    rows = db_session.execute(select(ContentChange)).scalars().all()
    assert len(rows) == 1
    assert rows[0].change_type == "added"
    assert rows[0].article_id == article.id
    assert rows[0].source_id == source.id
    assert rows[0].run_id == run.id
    assert rows[0].content_hash == "hash-abc"
    assert rows[0].topic_key == "https://x/a"
    assert rows[0].id >= 1  # BIGSERIAL assigned


def test_record_removals_writes_rows(db_session):
    source, run = _seed(db_session)

    # Create actual articles to reference in removals (to satisfy FK constraint)
    removed_articles = [
        Article(
            source_id=source.id, extraction_run_id=run.id, created_run_id=run.id,
            title="Gone1", source_url="https://x/gone1", topic_key="https://x/gone1",
            content_markdown="# G1", content_hash="hash-gone1",
        ),
        Article(
            source_id=source.id, extraction_run_id=run.id, created_run_id=run.id,
            title="Gone2", source_url="https://x/gone2", topic_key="https://x/gone2",
            content_markdown="# G2", content_hash="hash-gone2",
        ),
    ]
    db_session.add_all(removed_articles)
    db_session.flush()

    class Row:
        def __init__(self, id, topic_key):
            self.id = id
            self.topic_key = topic_key

    removed = [Row(removed_articles[0].id, "https://x/gone1"), Row(removed_articles[1].id, "https://x/gone2")]
    asyncio.run(
        change_log.record_removals(db_session, rows=removed, source_id=source.id, run_id=run.id)
    )
    db_session.commit()

    rows = db_session.execute(
        select(ContentChange).where(ContentChange.change_type == "removed").order_by(ContentChange.id)
    ).scalars().all()
    assert len(rows) == 2
    assert {r.topic_key for r in rows} == {"https://x/gone1", "https://x/gone2"}
    assert all(r.source_id == source.id and r.run_id == run.id for r in rows)
    assert all(r.content_hash is None for r in rows)
    assert {r.article_id for r in rows} == {removed_articles[0].id, removed_articles[1].id}


async def test_record_source_deletions_tombstones_live_articles_only(factory):
    from app.services import change_log
    from app.models.content_change import ContentChange, ChangeType
    from sqlalchemy import select
    from datetime import datetime, timezone

    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(name="P", vendor_id=v.id); s.add(p); await s.flush()
        src = DocumentationSource(name="S", base_url="https://s", product_id=p.id, source_type="web")
        s.add(src); await s.flush()
        live1 = Article(source_id=src.id, title="1", source_url="https://s/1",
                        topic_key="https://s/1", content_markdown="#1", content_hash="h1")
        live2 = Article(source_id=src.id, title="2", source_url="https://s/2",
                        topic_key="https://s/2", content_markdown="#2", content_hash="h2")
        gone = Article(source_id=src.id, title="3", source_url="https://s/3",
                       topic_key="https://s/3", content_markdown="#3", content_hash="h3",
                       removed_at=datetime.now(timezone.utc))
        s.add_all([live1, live2, gone]); await s.commit()
        sid, id1, id2 = src.id, live1.id, live2.id

    async with factory() as s:
        n = await change_log.record_source_deletions(s, source_ids=[sid])
        await s.commit()
        assert n == 2

    async with factory() as s:
        rows = (await s.execute(
            select(ContentChange).where(ContentChange.change_type == ChangeType.REMOVED.value)
        )).scalars().all()
        assert {r.article_id for r in rows} == {id1, id2}      # the removed one excluded
        assert all(r.run_id is None and r.source_id == sid for r in rows)
        assert all(r.topic_key for r in rows)
