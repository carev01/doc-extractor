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
