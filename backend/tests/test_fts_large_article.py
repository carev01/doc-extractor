"""A page larger than Postgres's 1 MB tsvector limit must still be stored.

Regression: the FTS GIN index expression runs to_tsvector on every insert, and
to_tsvector rejects input over 1,048,575 bytes ("string is too long for
tsvector"). An oversized page (e.g. a 5.8 MB document360 article) therefore
failed its insert and was silently skipped — data loss. The fix bounds the
index/query expression with left(…), so the full content is still stored while
FTS indexes a safe prefix.
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun, Article
from app.services.exporter import _TSV, _FTS_MAX_CHARS

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

# A distinctive term near the start (inside the indexed prefix) and one past the
# cap. Body padded well beyond 1 MB so the unbounded to_tsvector would reject it.
_EARLY = "zebrafishmarker"
_LATE = "ostrichmarker"


def _huge_markdown() -> str:
    # The tsvector byte limit is on the *output* (distinct lexemes + positions),
    # so the filler must be many UNIQUE words, not repetition — otherwise the
    # vector stays tiny and never trips the limit. ~200k unique tokens (~2.2 MB)
    # yields a >1 MB tsvector, reproducing the production failure.
    filler = " ".join(f"tok{i:07d}" for i in range(200_000))
    body = f"# Heading\n\n{_EARLY} " + filler + f"\n\n{_LATE}\n"
    assert len(body.encode()) > 1_048_575           # big input
    assert len(body) > _FTS_MAX_CHARS               # exceeds the left() cap
    return body


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # Build the FTS index exactly as the bound_fts_index migration does, using
        # the same _TSV the queries use — so the insert path exercises the index.
        await conn.execute(text(
            f"CREATE INDEX ix_articles_fts ON articles USING GIN ({_TSV})"))
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_source(s) -> uuid.UUID:
    v = Vendor(name="V"); s.add(v); await s.flush()
    p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
    src = DocumentationSource(product_id=p.id, name="D", base_url="https://d")
    s.add(src); await s.flush()
    run = ExtractionRun(source_id=src.id); s.add(run); await s.flush()
    return src.id, run.id


async def test_oversized_article_is_stored_and_searchable(factory):
    body = _huge_markdown()
    async with factory() as s:
        src_id, run_id = await _seed_source(s)
        art = Article(
            source_id=src_id, extraction_run_id=run_id, created_run_id=run_id,
            title="Big page", source_url="https://d/big", topic_key="https://d/big",
            content_markdown=body, content_hash="h",
        )
        s.add(art)
        await s.commit()                     # would raise ProgramLimitExceededError unbounded
        art_id = art.id

    async with factory() as s:
        stored = await s.get(Article, art_id)
        # Full content preserved (nothing truncated at rest), both markers present.
        assert stored is not None
        assert stored.content_markdown == body
        assert _LATE in stored.content_markdown

        # FTS finds it via a term inside the indexed prefix …
        pred = text(f"{_TSV} @@ plainto_tsquery('english', :q)").bindparams(q=_EARLY)
        hit = (await s.execute(select(Article.id).where(
            Article.id == art_id, pred))).scalar_one_or_none()
        assert hit == art_id

        # … but not via a term beyond the left() cap (index coverage is bounded).
        pred_late = text(f"{_TSV} @@ plainto_tsquery('english', :q)").bindparams(q=_LATE)
        miss = (await s.execute(select(Article.id).where(
            Article.id == art_id, pred_late))).scalar_one_or_none()
        assert miss is None


async def test_unbounded_tsvector_would_reject_the_same_input(factory):
    # Proves the bound is what fixes it: the pre-fix expression still errors.
    body = _huge_markdown()
    async with factory() as s:
        with pytest.raises(Exception) as ei:
            await s.execute(text("SELECT to_tsvector('english', :b)").bindparams(b=body))
        assert "tsvector" in str(ei.value).lower()
