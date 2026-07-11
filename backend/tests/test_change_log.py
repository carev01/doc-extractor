"""Unit tests for the content_changes write helpers (sync psycopg2 session)."""
import asyncio
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

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


@pytest.fixture(scope="function")
def db_session():
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    session = SyncSession()
    yield session
    session.rollback()
    session.close()


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
