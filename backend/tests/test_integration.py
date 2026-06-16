"""Integration tests for DocExtractor backend.

Uses a separate test database (docextractor_test).
Export engine tests use synchronous DB access to avoid asyncpg/pytest-asyncio
event-loop incompatibilities.
"""

import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, Article, TOCEntry
from app.services.exporter import ExportEngine

# Derive test DB URL from the configured sync URL — same host/credentials, different database.
TEST_DATABASE_URL_SYNC = settings.database_url_sync.rsplit("/", 1)[0] + "/docextractor_test"

sync_engine = create_engine(TEST_DATABASE_URL_SYNC, echo=False)
SyncSession = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


@pytest.fixture(scope="function")
def db_session():
    """Function-scoped synchronous session with clean tables."""
    # Drop and recreate tables for isolation
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)

    session = SyncSession()
    yield session
    session.rollback()
    session.close()

    # Clean up
    Base.metadata.drop_all(sync_engine)


# ── Export Engine Tests ──

def test_export_full(db_session):
    v = Vendor(name="ExportVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="ExportSource", base_url="https://docs.ex.com")
    db_session.add(s)
    db_session.flush()

    for i in range(5):
        a = Article(
            source_id=s.id, title=f"Article {i}",
            source_url=f"https://docs.ex.com/{i}",
            content_markdown=f"# Article {i}\n\nContent for article {i}.",
            sort_order=i, estimated_tokens=50, content_size_bytes=200,
        )
        db_session.add(a)
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(db_session, source_id=s.id)
    assert result["total_articles"] == 5
    assert result["file_count"] == 1
    assert result["files"][0]["article_count"] == 5

    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    files = os.listdir(export_dir)
    assert len(files) == 1
    filepath = os.path.join(export_dir, files[0])
    with open(filepath) as f:
        content = f.read()
    assert "ExportSource" in content
    assert "Article 0" in content
    assert "Article 4" in content


def test_export_partial_by_articles(db_session):
    v = Vendor(name="PartialVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="PartialSource", base_url="https://docs.px.com")
    db_session.add(s)
    db_session.flush()

    articles = []
    for i in range(10):
        a = Article(
            source_id=s.id, title=f"Article {i}",
            source_url=f"https://docs.px.com/{i}",
            content_markdown=f"# Article {i}\n\nContent {i}.",
            sort_order=i, estimated_tokens=50, content_size_bytes=200,
        )
        db_session.add(a)
        articles.append(a)
    db_session.commit()

    selected_ids = [articles[2].id, articles[5].id, articles[7].id]
    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id, article_ids=selected_ids
    )
    assert result["total_articles"] == 3
    assert result["file_count"] == 1


def test_export_by_topic_search(db_session):
    v = Vendor(name="TopicVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="TopicSource", base_url="https://docs.tp.com")
    db_session.add(s)
    db_session.flush()

    a1 = Article(
        source_id=s.id, title="Installation Guide",
        source_url="https://docs.tp.com/install",
        content_markdown="# Install\n\nRun pip install foo.",
        sort_order=0, estimated_tokens=20, content_size_bytes=100,
    )
    a2 = Article(
        source_id=s.id, title="API Reference",
        source_url="https://docs.tp.com/api",
        content_markdown="# API\n\nUse the /v1/foo endpoint.",
        sort_order=1, estimated_tokens=20, content_size_bytes=100,
    )
    a3 = Article(
        source_id=s.id, title="Troubleshooting",
        source_url="https://docs.tp.com/trouble",
        content_markdown="# Troubleshoot\n\nIf pip install fails, check Python version.",
        sort_order=2, estimated_tokens=20, content_size_bytes=100,
    )
    db_session.add_all([a1, a2, a3])
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id, topic_query="pip install"
    )
    assert result["total_articles"] == 2

    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    all_text = ""
    for fname in os.listdir(export_dir):
        with open(os.path.join(export_dir, fname)) as f:
            all_text += f.read()
    assert "Installation Guide" in all_text
    assert "Troubleshooting" in all_text
    assert "API Reference" not in all_text


def test_export_split_by_articles(db_session):
    v = Vendor(name="SplitVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="SplitSource", base_url="https://docs.sp.com")
    db_session.add(s)
    db_session.flush()

    for i in range(25):
        a = Article(
            source_id=s.id, title=f"Article {i:02d}",
            source_url=f"https://docs.sp.com/{i}",
            content_markdown=f"# Article {i:02d}\n\nFull content of article {i}.",
            sort_order=i, estimated_tokens=100, content_size_bytes=500,
        )
        db_session.add(a)
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id,
        split_by="articles", max_articles_per_file=10,
    )
    assert result["total_articles"] == 25
    assert result["file_count"] == 3
    assert result["files"][0]["article_count"] == 10
    assert result["files"][1]["article_count"] == 10
    assert result["files"][2]["article_count"] == 5

    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    for f in result["files"]:
        filepath = os.path.join(export_dir, f["filename"])
        with open(filepath) as fh:
            content = fh.read()
        assert "Full content of article" in content


def test_export_split_by_size(db_session):
    v = Vendor(name="SizeVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="SizeSource", base_url="https://docs.sz.com")
    db_session.add(s)
    db_session.flush()

    for i in range(5):
        size = 3000 if i == 2 else 500
        content = "x" * (size - 100)
        a = Article(
            source_id=s.id, title=f"Article {i}",
            source_url=f"https://docs.sz.com/{i}",
            content_markdown=f"# Article {i}\n\n{content}",
            sort_order=i, estimated_tokens=size // 4, content_size_bytes=size,
        )
        db_session.add(a)
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id,
        split_by="size", max_file_size_bytes=2000,
    )
    assert result["file_count"] >= 2
    large_file = [f for f in result["files"] if f["article_count"] == 1]
    assert len(large_file) >= 1
    assert large_file[0]["first_article_title"] == "Article 2"


def test_export_split_by_tokens(db_session):
    v = Vendor(name="TokenVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="TokenSource", base_url="https://docs.tk.com")
    db_session.add(s)
    db_session.flush()

    for i in range(8):
        a = Article(
            source_id=s.id, title=f"Article {i}",
            source_url=f"https://docs.tk.com/{i}",
            content_markdown=f"# Article {i}\n\nContent.",
            sort_order=i, estimated_tokens=30, content_size_bytes=120,
        )
        db_session.add(a)
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id,
        split_by="tokens", max_tokens_per_file=100,
    )
    assert result["file_count"] == 3
    assert result["total_articles"] == 8


def test_export_empty_selection(db_session):
    v = Vendor(name="EmptyVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="EmptySource", base_url="https://docs.em.com")
    db_session.add(s)
    db_session.commit()

    engine = ExportEngine()
    with pytest.raises(ValueError, match="No articles matched"):
        engine.export_sync(db_session, source_id=s.id)


def test_split_never_breaks_article(db_session):
    v = Vendor(name="InvariantVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="InvariantSource", base_url="https://docs.inv.com")
    db_session.add(s)
    db_session.flush()

    big = Article(
        source_id=s.id, title="Big Article",
        source_url="https://docs.inv.com/big",
        content_markdown="# Big\n\n" + ("x" * 10000),
        sort_order=0, estimated_tokens=2500, content_size_bytes=11000,
    )
    for i in range(5):
        a = Article(
            source_id=s.id, title=f"Small {i}",
            source_url=f"https://docs.inv.com/small/{i}",
            content_markdown=f"# Small {i}\n\nContent.",
            sort_order=i + 1, estimated_tokens=10, content_size_bytes=50,
        )
        db_session.add(a)
    db_session.add(big)
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id,
        split_by="size", max_file_size_bytes=5000,
    )

    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    big_found = False
    for f in result["files"]:
        filepath = os.path.join(export_dir, f["filename"])
        with open(filepath) as fh:
            content = fh.read()
        if "Big Article" in content:
            assert not big_found, "SPLIT VIOLATION: Big article in multiple files"
            big_found = True
            assert "x" * 10000 in content
    assert big_found, "Big article not found in any export file"


def test_toc_tree_structure(db_session):
    v = Vendor(name="TOCVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="TOCSource", base_url="https://docs.toc.com")
    db_session.add(s)
    db_session.flush()

    ch1 = TOCEntry(source_id=s.id, title="Chapter 1", level=0, sort_order=0, is_article=False)
    db_session.add(ch1)
    db_session.flush()

    sec1 = TOCEntry(
        source_id=s.id, title="Section 1.1", level=1, sort_order=1,
        parent_id=ch1.id, is_article=False,
    )
    db_session.add(sec1)
    db_session.flush()

    art = TOCEntry(
        source_id=s.id, title="Article A", level=2, sort_order=2,
        parent_id=sec1.id, is_article=True,
    )
    db_session.add(art)

    a = Article(
        source_id=s.id, toc_entry_id=art.id, title="Article A",
        source_url="https://docs.toc.com/a", content_markdown="# Hello",
        sort_order=0, estimated_tokens=10, content_size_bytes=100,
    )
    db_session.add(a)
    db_session.commit()

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    result = db_session.execute(
        select(TOCEntry)
        .where(TOCEntry.source_id == s.id, TOCEntry.parent_id == None)
        .options(selectinload(TOCEntry.children).selectinload(TOCEntry.children))
        .order_by(TOCEntry.sort_order)
    )
    roots = result.scalars().all()

    assert len(roots) == 1
    root = roots[0]
    assert root.title == "Chapter 1"
    assert len(root.children) == 1
    assert root.children[0].title == "Section 1.1"
    assert len(root.children[0].children) == 1
    assert root.children[0].children[0].title == "Article A"


# ── Firecrawl Service Tests ──

def test_firecrawl_service_available():
    from app.services.firecrawl import firecrawl_service, FirecrawlService
    assert firecrawl_service is not None
    assert hasattr(firecrawl_service, "extract_source")
    assert hasattr(firecrawl_service, "_firecrawl_map")
    assert hasattr(firecrawl_service, "_firecrawl_scrape")
    assert hasattr(firecrawl_service, "_download_image")
    assert hasattr(firecrawl_service, "_parse_toc_from_urls")


def test_toc_parsing():
    from app.services.firecrawl import firecrawl_service

    urls = [
        "https://docs.example.com/",
        "https://docs.example.com/getting-started",
        "https://docs.example.com/getting-started/installation",
        "https://docs.example.com/getting-started/configuration",
        "https://docs.example.com/api-reference",
        "https://docs.example.com/api-reference/authentication",
        "https://docs.example.com/api-reference/endpoints",
    ]

    toc = firecrawl_service._parse_toc_from_urls(urls, "https://docs.example.com")
    assert len(toc) == 7

    # "Getting Started" has children (installation, configuration) → non-article
    gs = [e for e in toc if e["title"] == "Getting Started"][0]
    assert gs["is_article"] is False

    # "Installation" is a leaf → article
    inst = [e for e in toc if e["title"] == "Installation"][0]
    assert inst["is_article"] is True

    # "API Reference" has children → non-article
    api = [e for e in toc if e["title"] == "Api Reference"][0]
    assert api["is_article"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
