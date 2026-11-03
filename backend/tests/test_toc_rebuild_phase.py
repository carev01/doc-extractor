"""The post-scrape TOC rebuild reports its own phase.

Oxygen's rebuild_toc stitches every page's nav fragment (149 MB for Rubrik RSC)
on a thread for minutes, logging nothing — while the run still said
``content_scraping`` at 4,990 / 4,992, so it looked hung on 2026-11-02. The
phase is committed as ``toc_rebuild`` *before* the rebuild starts, so the UI's
poller sees it while the thread is busy.
"""

import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from tests.db_reset import reset_schema
from app.models import Article, DocumentationSource, ExtractionRun, Product, Vendor
from app.models.extraction_run import RunStatus
from app.services import firecrawl as fc_mod
from app.services.firecrawl import FirecrawlService
from app.services.profiles.base import TocEntry

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
TEST_SYNC_URL = settings.database_url_sync.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

URL = "https://docs.example.com/guide/page{n}.html"


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(reset_schema)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class _RebuildingProfile:
    name = "stub"
    content_engine = "raw_http"
    render_engine = None

    def __init__(self, run_id):
        self.run_id = run_id
        self.seen_phase = None

    def content_config(self):
        return {}

    async def build_toc(self, root_url, scraper):
        return [TocEntry(title=f"P{n}", url=URL.format(n=n), level=0) for n in range(3)]

    def rebuild_toc(self, fragments, base_url):
        # Runs on a worker thread, like the real one: read what a poller sees.
        eng = create_engine(TEST_SYNC_URL, poolclass=NullPool)
        with eng.connect() as conn:
            self.seen_phase = conn.execute(
                text("select current_phase from extraction_runs where id = :id"),
                {"id": self.run_id},
            ).scalar_one()
        eng.dispose()
        return []


async def test_rebuild_runs_under_its_own_committed_phase(factory, monkeypatch):
    async with factory() as s:
        v = Vendor(name="Rubrik"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="RSC"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="RSC SaaS Docs", base_url=URL.format(n=0))
        s.add(src); await s.flush()
        for n in range(3):
            s.add(Article(source_id=src.id, title=f"P{n}", source_url=URL.format(n=n),
                          topic_key=f"k{n}", content_markdown="body",
                          toc_fragment="<ul><li>x</li></ul>"))
        run = ExtractionRun(source_id=src.id, status=RunStatus.PENDING)
        s.add(run); await s.commit()
        src_id, run_id = src.id, run.id

    profile = _RebuildingProfile(run_id)
    svc = FirecrawlService()
    monkeypatch.setattr(fc_mod, "async_session", factory)

    async def available():
        return True

    async def resolve(source, auth_cookies=None):
        return profile

    async def no_scrape(*a, **kw):
        return None

    monkeypatch.setattr(svc, "_check_available", available)
    monkeypatch.setattr(svc, "_resolve_profile", resolve)
    monkeypatch.setattr(svc, "_scrape_via_raw_http", no_scrape)

    async with factory() as s:
        await svc.extract_source(s, src_id, run_id=run_id)

    assert profile.seen_phase == "toc_rebuild"
