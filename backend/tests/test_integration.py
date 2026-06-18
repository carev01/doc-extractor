"""Integration tests for DocExtractor backend.

Uses a separate test database (docextractor_test).
Export engine tests use synchronous DB access to avoid asyncpg/pytest-asyncio
event-loop incompatibilities.
"""

import os
import sys
import uuid
import zipfile

import pytest
from pypdf import PdfReader
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, Article, TOCEntry
from app.models.image import ArticleImage
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
    md_files = [f for f in os.listdir(export_dir) if f.endswith(".md")]
    assert len(md_files) == 1
    # A self-contained zip bundle is produced alongside the markdown.
    assert result["zip_filename"] in os.listdir(export_dir)
    filepath = os.path.join(export_dir, md_files[0])
    with open(filepath) as f:
        content = f.read()
    assert "ExportSource" in content
    assert "Article 0" in content
    assert "Article 4" in content


def test_export_pdf_full(db_session):
    v = Vendor(name="PdfVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="PdfSource", base_url="https://docs.pdf.com")
    db_session.add(s)
    db_session.flush()
    for i in range(3):
        db_session.add(Article(
            source_id=s.id, title=f"Article {i}",
            source_url=f"https://docs.pdf.com/{i}",
            content_markdown=f"# Article {i}\n\nContent {i}.",
            sort_order=i, estimated_tokens=50, content_size_bytes=200,
        ))
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(db_session, source_id=s.id, format="pdf")
    assert result["file_count"] == 1
    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    pdf_files = [f for f in os.listdir(export_dir) if f.endswith(".pdf")]
    assert len(pdf_files) == 1
    # Self-contained: a PDF bundle has no images/ directory.
    assert not os.path.isdir(os.path.join(export_dir, "images"))
    with open(os.path.join(export_dir, pdf_files[0]), "rb") as f:
        assert f.read(5) == b"%PDF-"
    assert result["files"][0]["filename"].endswith(".pdf")


def test_export_pdf_split_produces_multiple_pdfs(db_session):
    v = Vendor(name="PdfSplitVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="PdfSplit", base_url="https://docs.ps.com")
    db_session.add(s)
    db_session.flush()
    for i in range(4):
        db_session.add(Article(
            source_id=s.id, title=f"A{i}", source_url=f"https://docs.ps.com/{i}",
            content_markdown=f"# A{i}\n\nx", sort_order=i,
            estimated_tokens=50, content_size_bytes=200,
        ))
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id, split_by="articles",
        max_articles_per_file=2, format="pdf",
    )
    assert result["file_count"] == 2
    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    pdf_files = [f for f in os.listdir(export_dir) if f.endswith(".pdf")]
    assert len(pdf_files) == 2


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
        if not fname.endswith(".md"):
            continue
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


def test_export_zip_bundles_images_with_relative_paths(db_session):
    """Export rewrites /media URLs to relative paths and bundles the image files."""
    v = Vendor(name="ImageVendor")
    db_session.add(v)
    db_session.flush()

    s = DocumentationSource(vendor_id=v.id, name="ImageSource", base_url="https://docs.img.com")
    db_session.add(s)
    db_session.flush()

    article = Article(
        source_id=s.id, title="With Image",
        source_url="https://docs.img.com/a",
        content_markdown="",  # set below once we know the article id
        sort_order=0, estimated_tokens=50, content_size_bytes=200,
    )
    db_session.add(article)
    db_session.flush()

    # Content references the served /media URL, as written by extraction.
    served_url = f"{settings.media_url_prefix}/{article.id}/pic.png"
    article.content_markdown = f"# With Image\n\n![diagram]({served_url})"

    db_session.add(ArticleImage(
        article_id=article.id,
        original_url="https://docs.img.com/pic.png",
        local_filename="pic.png",
        local_path=served_url,
        alt_text="diagram",
    ))
    db_session.commit()

    # Place the canonical image file on disk where the exporter expects it.
    media_root = os.path.abspath(settings.media_dir)
    img_dir = os.path.join(media_root, str(article.id))
    os.makedirs(img_dir, exist_ok=True)
    img_path = os.path.join(img_dir, "pic.png")
    with open(img_path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n_fake_png_bytes_")

    try:
        engine = ExportEngine()
        result = engine.export_sync(db_session, source_id=s.id)

        export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
        zip_path = os.path.join(export_dir, result["zip_filename"])
        assert os.path.isfile(zip_path)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            # The image is bundled at the relative path the markdown points to.
            assert f"images/{article.id}/pic.png" in names
            md_name = next(n for n in names if n.endswith(".md"))
            md_content = zf.read(md_name).decode("utf-8")

        # The markdown must use the relative path, not the served /media URL.
        assert f"images/{article.id}/pic.png" in md_content
        assert settings.media_url_prefix not in md_content
    finally:
        os.remove(img_path)


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


def test_topic_search_is_ranked_full_text(db_session):
    """Topic export uses Postgres FTS: stemmed matching, most-relevant first."""
    v = Vendor(name="FtsVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="FtsSource", base_url="https://docs.fts.com")
    db_session.add(s)
    db_session.flush()

    high = Article(
        source_id=s.id, title="Backups",
        source_url="https://docs.fts.com/1",
        content_markdown="Backup policy. Backup schedule. Backup retention and backup windows.",
        sort_order=0, estimated_tokens=10, content_size_bytes=80,
    )
    low = Article(
        source_id=s.id, title="Overview",
        source_url="https://docs.fts.com/2",
        content_markdown="A broad overview mentioning backup once amid networking, billing, and users.",
        sort_order=1, estimated_tokens=10, content_size_bytes=90,
    )
    unrelated = Article(
        source_id=s.id, title="Networking",
        source_url="https://docs.fts.com/3",
        content_markdown="Routing, firewalls, and VPN tunnels.",
        sort_order=2, estimated_tokens=10, content_size_bytes=60,
    )
    db_session.add_all([high, low, unrelated])
    db_session.commit()

    engine = ExportEngine()
    # Plural query proves stemming (a substring ILIKE on "backups" would match none).
    result = engine.export_sync(db_session, source_id=s.id, topic_query="backups")

    assert result["total_articles"] == 2  # unrelated excluded
    # Single file, articles ordered by relevance: the backup-dense page first.
    assert result["files"][0]["first_article_title"] == "Backups"
    assert result["files"][0]["last_article_title"] == "Overview"


def _chapter_fixture(db_session):
    """Two chapters; chapter 2 has two articles under a subsection.

    Sizes/order are chosen so a greedy split would slice chapter 2 across files.
    """
    v = Vendor(name="ChapVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="ChapSource", base_url="https://docs.ch.com")
    db_session.add(s)
    db_session.flush()

    ch1 = TOCEntry(source_id=s.id, title="Chapter 1", level=0, sort_order=0)
    ch2 = TOCEntry(source_id=s.id, title="Chapter 2", level=0, sort_order=1)
    db_session.add_all([ch1, ch2])
    db_session.flush()
    sec2 = TOCEntry(
        source_id=s.id, title="Section 2.1", level=1, sort_order=2,
        parent_id=ch2.id, is_article=False,
    )
    db_session.add(sec2)
    db_session.flush()

    def art(title, toc_id, order):
        return Article(
            source_id=s.id, toc_entry_id=toc_id, title=title,
            source_url=f"https://docs.ch.com/{title}",
            content_markdown="x" * 480, sort_order=order,
            estimated_tokens=120, content_size_bytes=500,
        )

    # A1 ∈ Chapter 1; A2, A3 ∈ Chapter 2 (via Section 2.1)
    db_session.add_all([
        art("A1", ch1.id, 0),
        art("A2", sec2.id, 1),
        art("A3", sec2.id, 2),
    ])
    db_session.commit()
    return s


def test_split_chapter_aware_keeps_chapter_together(db_session):
    s = _chapter_fixture(db_session)
    engine = ExportEngine()

    # Greedy (no chapter awareness) packs A1+A2, pushing A3 alone — splitting ch2.
    plain = engine.export_sync(
        db_session, source_id=s.id, split_by="size", max_file_size_bytes=1200
    )
    assert plain["files"][0]["last_article_title"] == "A2"
    assert plain["files"][1]["first_article_title"] == "A3"

    # Chapter-aware starts a new file at the chapter boundary: A1 | A2+A3.
    chap = engine.export_sync(
        db_session, source_id=s.id, split_by="size",
        max_file_size_bytes=1200, respect_chapters=True,
    )
    assert chap["file_count"] == 2
    assert chap["files"][0]["first_article_title"] == "A1"
    assert chap["files"][0]["last_article_title"] == "A1"
    assert chap["files"][1]["first_article_title"] == "A2"
    assert chap["files"][1]["last_article_title"] == "A3"


def test_split_chapter_larger_than_limit_splits_internally(db_session):
    """A chapter bigger than one file is split internally — articles stay intact."""
    v = Vendor(name="BigChapVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="BigChapSource", base_url="https://docs.bc.com")
    db_session.add(s)
    db_session.flush()
    ch = TOCEntry(source_id=s.id, title="Solo Chapter", level=0, sort_order=0)
    db_session.add(ch)
    db_session.flush()
    for i in range(3):
        db_session.add(Article(
            source_id=s.id, toc_entry_id=ch.id, title=f"P{i}",
            source_url=f"https://docs.bc.com/{i}", content_markdown="y" * 480,
            sort_order=i, estimated_tokens=120, content_size_bytes=500,
        ))
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id, split_by="size",
        max_file_size_bytes=1000, respect_chapters=True,
    )
    # 1500 bytes of chapter / 1000 limit → 2 files, all 3 articles preserved whole.
    assert result["file_count"] == 2
    assert sum(f["article_count"] for f in result["files"]) == 3


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


def test_export_pdf_merges_per_chapter(db_session, monkeypatch):
    import app.services.exporter as exporter_mod
    monkeypatch.setattr(exporter_mod, "_RENDER_CHUNK", 1)  # force one chunk per article

    v = Vendor(name="MergeVendor")
    db_session.add(v); db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="MergeSrc", base_url="https://m.com")
    db_session.add(s); db_session.flush()
    # Two top-level chapters, 2 articles each.
    ch1 = TOCEntry(source_id=s.id, title="Chapter 1", url=None, level=0, sort_order=0, is_article=False)
    ch2 = TOCEntry(source_id=s.id, title="Chapter 2", url=None, level=0, sort_order=3, is_article=False)
    db_session.add_all([ch1, ch2]); db_session.flush()
    arts = []
    for ci, ch in enumerate((ch1, ch2)):
        for j in range(2):
            t = TOCEntry(source_id=s.id, title=f"c{ci}a{j}", url=f"https://m.com/{ci}/{j}",
                         level=1, sort_order=ci * 10 + j + 1, is_article=True, parent_id=ch.id)
            db_session.add(t); db_session.flush()
            arts.append(Article(
                source_id=s.id, toc_entry_id=t.id, title=f"c{ci}a{j}",
                source_url=f"https://m.com/{ci}/{j}", content_markdown=f"# c{ci}a{j}\n\nbody",
                sort_order=ci * 10 + j + 1, estimated_tokens=50, content_size_bytes=200,
            ))
    db_session.add_all(arts); db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(db_session, source_id=s.id, format="pdf")
    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    pdfs = [f for f in os.listdir(export_dir) if f.endswith(".pdf")]
    assert len(pdfs) == 1
    reader = PdfReader(os.path.join(export_dir, pdfs[0]))
    # header page + one page per article chunk (4) merged.
    assert len(reader.pages) >= 5
    # No leftover temp chunk PDFs.
    assert not any(f.startswith("_chunk") for f in os.listdir(export_dir))


# ── Firecrawl Service Tests ──

def test_firecrawl_service_available():
    from app.services.firecrawl import firecrawl_service
    assert firecrawl_service is not None
    assert hasattr(firecrawl_service, "extract_source")
    assert hasattr(firecrawl_service, "_firecrawl_request")
    assert hasattr(firecrawl_service, "_scrape_nav_html")
    assert hasattr(firecrawl_service, "_scrape_article")
    assert hasattr(firecrawl_service, "_build_toc_recursive")
    assert hasattr(firecrawl_service, "_parse_nav_items")
    assert hasattr(firecrawl_service, "_download_image")


def test_nav_item_parsing():
    """_parse_nav_items extracts ordered items from a nav <ul>."""
    from bs4 import BeautifulSoup
    from app.services.firecrawl import firecrawl_service

    html = """
    <ul class="nav-group nav-group-root">
      <li class="nav-row">
        <div class="nav-item nav-doc" data-is-parent="">
          <a href="https://docs.example.com/section-a">Section A</a>
        </div>
      </li>
      <li class="nav-row">
        <div class="nav-item nav-doc">
          <a href="https://docs.example.com/page-b">Page B</a>
        </div>
      </li>
    </ul>
    """
    soup = BeautifulSoup(html, "html.parser")
    ul = soup.find("ul")
    items = firecrawl_service._parse_nav_items(ul)

    assert len(items) == 2
    assert items[0]["title"] == "Section A"
    assert items[0]["url"] == "https://docs.example.com/section-a"
    assert items[0]["is_parent"] is True
    assert items[1]["title"] == "Page B"
    assert items[1]["is_parent"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
