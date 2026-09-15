"""A release that re-mints the vendor's build id must not duplicate the source.

Mirrors tests/test_versioning_match.py, but for the harder shape: the URL changes
in TWO places (the version segment and a per-release build id that occurs twice),
so the article has to be recognised across a boundary where neither its key nor
its URL matches what the new run derives.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import Base
from app.models import Article, ArticleVersion
from app.services.versioning import derive_topic_key
from tests.helpers_versioning import make_service_and_source, _make_run

TMPL = (
    "https://docs.cohesity.com/docs/netbackup/{version}"
    "/103228346-{rev}-0/v95650213-{rev}"
)
OLD = (
    "https://docs.cohesity.com/docs/netbackup/11.2"
    "/103228346-171368441-0/v95650213-171368441"
)
NEW = (
    "https://docs.cohesity.com/docs/netbackup/11.2.0.1"
    "/103228346-173151032-0/v95650213-173151032"
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session():
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


async def _extract(svc, db, source, url, key, body, title="About NetBackup"):
    run = await _make_run(db, source)
    return await svc.process_article_result(
        db=db, source_id=source.id, run_id=run.id, url=url, topic_key=key,
        markdown_content=body, doc_html="", toc_entry_id=None,
        sort_order=0, title=title,
    )


async def test_release_bump_updates_in_place_and_archives_the_old_content(db_session):
    svc, source = await make_service_and_source(
        db_session, url_template=TMPL, version="11.2"
    )
    key_old = derive_topic_key(OLD, TMPL, "11.2")
    await _extract(svc, db_session, source, OLD, key_old, "11.2 body")

    key_new = derive_topic_key(NEW, TMPL, "11.2.0.1")
    assert key_new == key_old, "the whole point: one key spans both releases"

    await _extract(svc, db_session, source, NEW, key_new, "11.2.0.1 body")

    arts = (
        await db_session.execute(select(Article).where(Article.source_id == source.id))
    ).scalars().all()
    assert len(arts) == 1, "a re-minted build id must not create a second article"
    assert arts[0].source_url == NEW
    versions = (
        await db_session.execute(
            select(ArticleVersion).where(ArticleVersion.article_id == arts[0].id)
        )
    ).scalars().all()
    assert len(versions) == 1, "the 11.2 content should be archived, not lost"


async def test_a_corpus_stored_before_rev_existed_heals_instead_of_duplicating(
    db_session,
):
    """The live NetBackup case: articles were keyed when the template had only
    {version}, so their stored key carries 11.2's build id. At 11.2.0.1 neither
    the key nor the URL matches — only the placeholder-wildcard pattern can link
    them. Without it the source retires every article and re-adds it."""
    svc, source = await make_service_and_source(
        db_session, url_template=TMPL, version="11.2"
    )
    # The key a {version}-only template produced: build id still literal.
    legacy_key = OLD.replace("11.2/", "{version}/", 1)
    assert "171368441" in legacy_key
    await _extract(svc, db_session, source, OLD, legacy_key, "11.2 body")

    key_new = derive_topic_key(NEW, TMPL, "11.2.0.1")
    assert key_new != legacy_key

    await _extract(svc, db_session, source, NEW, key_new, "11.2.0.1 body changed")

    arts = (
        await db_session.execute(select(Article).where(Article.source_id == source.id))
    ).scalars().all()
    assert len(arts) == 1, "must heal by URL shape, not duplicate"
    assert arts[0].topic_key == key_new, "and normalise the stale key going forward"
    assert arts[0].removed_at is None


async def test_an_unrelated_publication_is_not_adopted_by_the_wildcard(db_session):
    """The wildcard spans the build id — it must not span the publication or
    topic id, or two different guides would collapse into one article."""
    svc, source = await make_service_and_source(
        db_session, url_template=TMPL, version="11.2"
    )
    await _extract(svc, db_session, source, OLD, derive_topic_key(OLD, TMPL, "11.2"), "a")

    other = (
        "https://docs.cohesity.com/docs/netbackup/11.2.0.1"
        "/169433500-172152080-0/v131282878-172152080"
    )
    await _extract(
        svc, db_session, source, other,
        derive_topic_key(other, TMPL, "11.2.0.1"), "b", title="Other guide",
    )
    arts = (
        await db_session.execute(select(Article).where(Article.source_id == source.id))
    ).scalars().all()
    assert len(arts) == 2, "different publications are different articles"
