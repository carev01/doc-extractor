"""Tests for incremental extraction data layer and change-detection logic.

These use synchronous psycopg2 sessions (same pattern as test_integration)
against the docextractor_test DB to avoid asyncpg/pytest-asyncio
event-loop conflicts. The async Firecrawl scrape is not exercised here;
instead we validate the hashing, version-snapshot, and counter semantics
that the incremental path relies on.
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import (
    Vendor,
    Product,
    DocumentationSource,
    Article,
    ArticleVersion,
    ExtractionRun,
)
from app.services.firecrawl import compute_content_hash

# Derive test DB URL from the configured sync URL — same host/credentials, different database.
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
    Base.metadata.drop_all(sync_engine)


def _make_source(db):
    v = Vendor(name="IncVendor")
    db.add(v)
    db.flush()
    s_prod = Product(vendor_id=v.id, name="P")
    db.add(s_prod)
    db.flush()
    s = DocumentationSource(
        product_id=s_prod.id, name="IncSource", base_url="https://docs.inc.com"
    )
    db.add(s)
    db.flush()
    return s


def _classify(db, source_id, url, markdown):
    """Mirror the incremental decision in FirecrawlService.extract_source.

    Returns one of "unchanged", "updated", "inserted" and applies the
    same DB effects (version snapshot on update).
    """
    content_hash = compute_content_hash(markdown)
    existing = db.execute(
        select(Article).where(
            Article.source_id == source_id, Article.source_url == url
        )
    ).scalar_one_or_none()

    if existing is not None and existing.content_hash == content_hash:
        return "unchanged"

    if existing is not None:
        db.add(
            ArticleVersion(
                article_id=existing.id,
                content_markdown=existing.content_markdown,
                content_hash=existing.content_hash,
            )
        )
        existing.content_markdown = markdown
        existing.content_hash = content_hash
        db.flush()
        return "updated"

    db.add(
        Article(
            source_id=source_id,
            title="T",
            source_url=url,
            topic_key=url,
            content_markdown=markdown,
            content_hash=content_hash,
            sort_order=0,
            estimated_tokens=len(markdown) // 4,
            content_size_bytes=len(markdown.encode("utf-8")),
        )
    )
    db.flush()
    return "inserted"


def test_content_hash_is_sha256_and_stable():
    h1 = compute_content_hash("# Hello\n\nworld")
    h2 = compute_content_hash("# Hello\n\nworld")
    assert h1 == h2
    assert len(h1) == 64
    assert h1 != compute_content_hash("# Hello\n\nplanet")


def test_first_run_inserts(db_session):
    s = _make_source(db_session)
    assert _classify(db_session, s.id, "https://docs.inc.com/a", "v1") == "inserted"
    db_session.commit()

    arts = db_session.execute(select(Article)).scalars().all()
    assert len(arts) == 1
    assert arts[0].content_hash == compute_content_hash("v1")


def test_unchanged_content_is_skipped(db_session):
    s = _make_source(db_session)
    _classify(db_session, s.id, "https://docs.inc.com/a", "same content")
    db_session.commit()

    # Second run with identical content → unchanged, no new version row.
    assert (
        _classify(db_session, s.id, "https://docs.inc.com/a", "same content")
        == "unchanged"
    )
    db_session.commit()

    assert len(db_session.execute(select(Article)).scalars().all()) == 1
    assert len(db_session.execute(select(ArticleVersion)).scalars().all()) == 0


def test_changed_content_updates_and_snapshots_old(db_session):
    s = _make_source(db_session)
    _classify(db_session, s.id, "https://docs.inc.com/a", "original")
    db_session.commit()

    assert (
        _classify(db_session, s.id, "https://docs.inc.com/a", "revised")
        == "updated"
    )
    db_session.commit()

    arts = db_session.execute(select(Article)).scalars().all()
    assert len(arts) == 1
    assert arts[0].content_markdown == "revised"
    assert arts[0].content_hash == compute_content_hash("revised")

    versions = db_session.execute(select(ArticleVersion)).scalars().all()
    assert len(versions) == 1
    # The snapshot preserves the OLD content before overwriting.
    assert versions[0].content_markdown == "original"
    assert versions[0].content_hash == compute_content_hash("original")
    assert versions[0].article_id == arts[0].id


def test_extraction_run_counters(db_session):
    s = _make_source(db_session)
    # Seed two articles from a prior run.
    _classify(db_session, s.id, "https://docs.inc.com/a", "A1")
    _classify(db_session, s.id, "https://docs.inc.com/b", "B1")
    db_session.commit()

    run = ExtractionRun(source_id=s.id)
    db_session.add(run)
    db_session.flush()
    assert run.articles_unchanged == 0
    assert run.articles_updated == 0

    counts = {"unchanged": 0, "updated": 0, "inserted": 0}
    # a unchanged, b changed, c new
    for url, md in [
        ("https://docs.inc.com/a", "A1"),
        ("https://docs.inc.com/b", "B2"),
        ("https://docs.inc.com/c", "C1"),
    ]:
        counts[_classify(db_session, s.id, url, md)] += 1

    run.articles_unchanged = counts["unchanged"]
    run.articles_updated = counts["updated"]
    run.articles_extracted = counts["inserted"]
    db_session.commit()

    assert counts == {"unchanged": 1, "updated": 1, "inserted": 1}
    assert run.articles_unchanged == 1
    assert run.articles_updated == 1
    assert run.articles_extracted == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
