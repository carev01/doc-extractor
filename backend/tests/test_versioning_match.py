# backend/tests/test_versioning_match.py
# Sync-DB test mirroring tests/test_versions.py harness: build a source with an
# article at v10.0, then re-run process_article_result with the v11.0 URL but the
# SAME topic_key and assert the same article row is updated (history preserved).
import os, sys, uuid, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import Base
from app.models import Article, ArticleVersion
from app.services.versioning import derive_topic_key

# Reuse the async-session + FirecrawlService fixtures from the existing suite.
from tests.helpers_versioning import make_service_and_source, _make_run  # see Step 5

TMPL = "https://docs.example.com/UDP/Available/{version}/ENU/SolG/install.htm"

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session():
    """Create a fresh async session against docextractor_test for this test."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_bump_matches_by_topic_key_and_appends_version(db_session):
    svc, source = await make_service_and_source(db_session, url_template=TMPL, version="10.0")
    run = await _make_run(db_session, source)  # helper: PENDING run for source
    key = derive_topic_key(TMPL.replace("{version}", "10.0"), TMPL, "10.0")
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run.id,
        url=TMPL.replace("{version}", "10.0"), topic_key=key,
        markdown_content="v10 body", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    art = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalar_one()
    assert art.topic_key == key and "10.0" in art.source_url

    # Same topic, new version URL — must update the SAME row + add a version.
    run2 = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run2.id,
        url=TMPL.replace("{version}", "11.0"), topic_key=key,
        markdown_content="v11 body", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    arts = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalars().all()
    assert len(arts) == 1                    # same row, not a new article
    assert "11.0" in arts[0].source_url       # source_url advanced
    versions = (await db_session.execute(
        select(ArticleVersion).where(ArticleVersion.article_id == arts[0].id)
    )).scalars().all()
    assert len(versions) == 1                 # the v10 snapshot was archived


async def test_url_fallback_heals_drifted_literal_key(db_session):
    # A stored article keyed by its LITERAL version (as happens when a run keyed
    # pages before url_template was set) must NOT be duplicated when it is later
    # re-extracted with the correct {version} key — it is matched by source_url
    # and its key is normalised. (Regression: the CommCell/Arcserve duplication.)
    svc, source = await make_service_and_source(db_session, url_template=TMPL, version="10.0")
    url = TMPL.replace("{version}", "10.0")

    run1 = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run1.id,
        url=url, topic_key=url,  # literal key (drifted)
        markdown_content="body v1", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    art = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalar_one()
    assert art.topic_key == url  # stored literally

    key = derive_topic_key(url, TMPL, "10.0")
    assert "{version}" in key and key != url
    run2 = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run2.id,
        url=url, topic_key=key,  # correct version-independent key
        markdown_content="body v2 changed", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    arts = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalars().all()
    assert len(arts) == 1               # matched by URL — no duplicate
    assert arts[0].topic_key == key     # key normalised to {version}


async def test_bump_heals_drifted_literal_key_across_version_change(db_session):
    # The hard case both earlier fallbacks miss: the stored key is a drifted
    # LITERAL-version key (pre-fix run) AND the version bumps, so the URL changes
    # too. Neither topic_key nor exact-URL matches — only the version-independent
    # URL-pattern fallback can link new→old. Must update in place (history kept),
    # not duplicate. (Regression: the Commvault Cloud / CommCell 11.44→11.46 all-new.)
    svc, source = await make_service_and_source(db_session, url_template=TMPL, version="10.0")
    old_url = TMPL.replace("{version}", "10.0")

    run1 = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run1.id,
        url=old_url, topic_key=old_url,  # LITERAL key (drifted, pre-fix)
        markdown_content="body v10", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )

    new_url = TMPL.replace("{version}", "11.0")
    key = derive_topic_key(new_url, TMPL, "11.0")
    assert "{version}" in key
    run2 = await _make_run(db_session, source)
    outcome = await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run2.id,
        url=new_url, topic_key=key,  # correct templated key + bumped URL
        markdown_content="body v11 changed", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    assert outcome == "updated"  # matched the drifted v10 row, not re-created
    arts = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalars().all()
    assert len(arts) == 1                    # no duplicate
    assert arts[0].topic_key == key          # key normalised to {version}
    assert "11.0" in arts[0].source_url      # advanced to the new version
    versions = (await db_session.execute(
        select(ArticleVersion).where(ArticleVersion.article_id == arts[0].id)
    )).scalars().all()
    assert len(versions) == 1                # v10 snapshot archived → history preserved


async def test_bump_fallback_leaves_genuinely_new_page_as_new(db_session):
    # The version-bump fallback must not over-match: a page that only exists at the
    # new version (no counterpart at the old version) has no URL-pattern match and
    # must be created as new, never adopting an unrelated page.
    svc, source = await make_service_and_source(db_session, url_template=TMPL, version="10.0")
    old_url = TMPL.replace("{version}", "10.0")
    run1 = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run1.id,
        url=old_url, topic_key=old_url,
        markdown_content="body v10", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )

    # A different page path, only present at v11.0.
    other_tmpl = "https://docs.example.com/UDP/Available/{version}/ENU/SolG/brand_new.htm"
    other_url = other_tmpl.replace("{version}", "11.0")
    key = derive_topic_key(other_url, other_tmpl, "11.0")
    run2 = await _make_run(db_session, source)
    outcome = await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run2.id,
        url=other_url, topic_key=key,
        markdown_content="brand new page", doc_html="", toc_entry_id=None,
        sort_order=1, title="Brand New",
    )
    assert outcome == "new"  # no false adoption of the Install page
    arts = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalars().all()
    assert len(arts) == 2


async def test_url_fallback_skipped_when_url_is_ambiguous(db_session):
    # PDF-safety: when several live articles share a source_url (outline sections
    # on the same #page), the URL fallback must NOT fire — otherwise a section
    # could overwrite a sibling. A drifted-key page falls through to a new row
    # instead, leaving the existing siblings' content intact.
    svc, source = await make_service_and_source(db_session, url_template=TMPL, version="10.0")
    shared = "https://docs.example.com/UDP/Available/10.0/ENU/SolG/manual.pdf#page=5"
    a = Article(source_id=source.id, title="Sec A", source_url=shared, topic_key="sec-a",
                content_markdown="AAA", content_hash="ha")
    b = Article(source_id=source.id, title="Sec B", source_url=shared, topic_key="sec-b",
                content_markdown="BBB", content_hash="hb")
    db_session.add_all([a, b]); await db_session.commit()

    run = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run.id,
        url=shared, topic_key="sec-c-new",  # doesn't match either sibling
        markdown_content="CCC", doc_html="", toc_entry_id=None, sort_order=0, title="Sec C",
    )
    # Neither sibling was overwritten; a new row was created instead.
    got_a = (await db_session.execute(select(Article).where(Article.id == a.id))).scalar_one()
    got_b = (await db_session.execute(select(Article).where(Article.id == b.id))).scalar_one()
    assert got_a.content_markdown == "AAA" and got_b.content_markdown == "BBB"
    total = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalars().all()
    assert len(total) == 3
