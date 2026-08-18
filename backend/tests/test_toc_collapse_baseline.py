"""The TOC-collapse guard inside a real ``extract_source`` run.

Two behaviours the pure ``_toc_collapsed``/``_collapse_baseline`` unit tests can't
cover, because both depend on what the run reads from the database:

1. The baseline is the *lower* of the live-article count and the last completed
   extraction's page total. Arcserve "Agent for Linux Guide" held 518 live articles
   for 259 distinct URLs (each page stored twice by the pre-#189 raw_http
   literal-key bug), so a healthy 255-page TOC read as < 50% of 518 and every run
   aborted — the guard blocking the very run that would retire the duplicates.
2. ``run.allow_toc_collapse`` ("Extract anyway") lets an operator proceed when the
   doc set genuinely shrank, without weakening the guard for any other run.

No network: the profile and the content phase are stubbed, so the run reaches the
guard and stops (or proceeds into a no-op content phase).
"""

import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Article, DocumentationSource, ExtractionRun, Product, Vendor
from app.models.extraction_run import RunStatus
from app.models.toc import TOCEntry
from app.services import firecrawl as fc_mod
from app.services.firecrawl import (
    TOC_COLLAPSE_PREFIX, FirecrawlService, TocCollapseError,
)
from app.services.profiles.base import TocEntry

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

BASE = "https://docs.example.com/guide/11.0/page{n}.htm"


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


class _StubProfile:
    """A profile that yields a fixed TOC and scrapes over the raw_http path (which
    the test stubs out), so no Firecrawl/Browserless call is ever made."""

    name = "stub"
    content_engine = "raw_http"
    render_engine = None

    def __init__(self, pages: int):
        self._pages = pages

    def content_config(self):
        return {}

    async def build_toc(self, root_url, scraper):
        return [
            TocEntry(title=f"Page {n}", url=BASE.format(n=n), level=0, is_article=True)
            for n in range(self._pages)
        ]


def _service(monkeypatch, pages: int, factory) -> FirecrawlService:
    svc = FirecrawlService()
    # extract_source opens its own sessions (TOC checkpoint, control poller) from
    # the module-level async_session, whose engine binds to whichever event loop
    # touched it first. Point it at this test's per-loop factory, or a second test
    # that gets past the guard trips asyncpg's "attached to a different loop".
    monkeypatch.setattr(fc_mod, "async_session", factory)

    async def available():
        return True

    async def resolve(source, auth_cookies=None):
        return _StubProfile(pages)

    async def no_scrape(*a, **kw):
        return None

    monkeypatch.setattr(svc, "_check_available", available)
    monkeypatch.setattr(svc, "_resolve_profile", resolve)
    monkeypatch.setattr(svc, "_scrape_via_raw_http", no_scrape)
    return svc


async def _fixture_source(
    factory, *, live_articles: int, distinct_urls: int, last_run_total: int | None
):
    """A source with ``live_articles`` live rows spread over ``distinct_urls`` URLs
    (so >1 per URL reproduces the duplicated corpus), plus an optional prior
    COMPLETED run recording ``last_run_total`` pages.
    """
    async with factory() as s:
        v = Vendor(name="Arcserve"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="UDP", version="11.0"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="Agent for Linux Guide",
            base_url=BASE.format(n=0),
            url_template=BASE.format(n=0).replace("11.0", "{version}"),
        )
        s.add(src); await s.flush()
        for i in range(live_articles):
            s.add(Article(
                source_id=src.id, title=f"Page {i}",
                source_url=BASE.format(n=i % distinct_urls),
                topic_key=f"key-{i}", content_markdown="body",
            ))
        if last_run_total is not None:
            s.add(ExtractionRun(
                source_id=src.id, status=RunStatus.COMPLETED, kind="extract",
                articles_total=last_run_total, articles_extracted=last_run_total,
            ))
        await s.commit()
        return src.id


async def _queue_run(factory, source_id, *, allow_toc_collapse=False):
    async with factory() as s:
        run = ExtractionRun(
            source_id=source_id, status=RunStatus.PENDING,
            allow_toc_collapse=allow_toc_collapse,
        )
        s.add(run); await s.commit()
        return run.id


async def test_duplicated_corpus_no_longer_trips_the_guard(factory, monkeypatch):
    # The Arcserve numbers exactly: 518 live rows over 259 URLs, last good run 259,
    # new TOC 255. 255 < 50% of 518 (the old baseline) but well above 50% of 259.
    src_id = await _fixture_source(
        factory, live_articles=518, distinct_urls=259, last_run_total=259
    )
    run_id = await _queue_run(factory, src_id)
    svc = _service(monkeypatch, factory=factory, pages=255)

    async with factory() as s:
        run = await svc.extract_source(s, src_id, run_id=run_id)

    assert run.status != RunStatus.FAILED, run.error_message
    async with factory() as s:
        persisted = (await s.execute(
            select(TOCEntry).where(TOCEntry.source_id == src_id)
        )).scalars().all()
    assert len(persisted) == 255      # got past the guard and rebuilt the TOC


async def test_genuine_collapse_still_aborts_before_any_removal(factory, monkeypatch):
    # The protection itself: an empty/failed nav yields a single Index page, which
    # is below half of *either* signal. Nothing may be removed or rebuilt.
    src_id = await _fixture_source(
        factory, live_articles=518, distinct_urls=259, last_run_total=259
    )
    run_id = await _queue_run(factory, src_id)
    svc = _service(monkeypatch, factory=factory, pages=1)

    async with factory() as s:
        with pytest.raises(TocCollapseError, match=TOC_COLLAPSE_PREFIX):
            await svc.extract_source(s, src_id, run_id=run_id)
        await s.commit()   # persist the FAILED bookkeeping, as the worker does

    async with factory() as s:
        run = await s.get(ExtractionRun, run_id)
        assert run.status == RunStatus.FAILED
        assert (run.error_message or "").startswith(TOC_COLLAPSE_PREFIX)
        still_live = (await s.execute(
            select(Article).where(
                Article.source_id == src_id, Article.removed_at.is_(None)
            )
        )).scalars().all()
        toc = (await s.execute(
            select(TOCEntry).where(TOCEntry.source_id == src_id)
        )).scalars().all()
    assert len(still_live) == 518     # nothing retired
    assert toc == []                  # TOC never rebuilt


async def test_allow_toc_collapse_overrides_the_guard(factory, monkeypatch):
    # "Extract anyway": the same collapse that fails above must proceed when the
    # operator set the per-run override.
    src_id = await _fixture_source(
        factory, live_articles=400, distinct_urls=400, last_run_total=400
    )
    run_id = await _queue_run(factory, src_id, allow_toc_collapse=True)
    svc = _service(monkeypatch, factory=factory, pages=3)

    async with factory() as s:
        run = await svc.extract_source(s, src_id, run_id=run_id)

    assert run.status != RunStatus.FAILED, run.error_message
    async with factory() as s:
        toc = (await s.execute(
            select(TOCEntry).where(TOCEntry.source_id == src_id)
        )).scalars().all()
    assert len(toc) == 3


async def test_baseline_ignores_non_extract_and_failed_runs(factory, monkeypatch):
    # Only completed kind="extract" runs carry a page total; an enrich/escalate run
    # (articles_total 0) or a failed run must not become the baseline — otherwise a
    # zero baseline would silently disable the guard.
    src_id = await _fixture_source(
        factory, live_articles=300, distinct_urls=300, last_run_total=None
    )
    async with factory() as s:
        s.add(ExtractionRun(source_id=src_id, status=RunStatus.COMPLETED,
                            kind="enrich", articles_total=0))
        s.add(ExtractionRun(source_id=src_id, status=RunStatus.FAILED,
                            kind="extract", articles_total=2))
        await s.commit()
    run_id = await _queue_run(factory, src_id)
    svc = _service(monkeypatch, factory=factory, pages=5)

    # Falls back to the live count (300), so 5 pages still trips the guard.
    async with factory() as s:
        with pytest.raises(TocCollapseError, match="baseline of 300"):
            await svc.extract_source(s, src_id, run_id=run_id)
