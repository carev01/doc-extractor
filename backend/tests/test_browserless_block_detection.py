"""A Browserless render that returns an empty content selector but a WAF
"Access Denied" body (Akamai on Dell manuals) must be recorded as *blocked* —
surfacing a bot-protection warning + feeding the blocked-page retry — not
silently skipped as an empty page.
"""
import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.browserless as browserless_mod
from app.core.config import settings
from app.core.database import Base
from app.models import Article, DocumentationSource, ExtractionRun, Product, Vendor
from app.services.firecrawl import firecrawl_service, _BLOCKED_MSG

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"

_AKAMAI = ('Access Denied You don\'t have permission to access this server. '
           'Reference #18.9260 https://errors.edgesuite.net/18.9260')


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_browserless_block_page_recorded_not_skipped(db, monkeypatch):
    v = Vendor(name="Dell"); db.add(v); await db.flush()
    p = Product(vendor_id=v.id, name="PP"); db.add(p); await db.flush()
    src = DocumentationSource(product_id=p.id, name="Guide",
                              base_url="https://www.dell.com/support/manuals/x",
                              source_type="web")
    db.add(src); await db.flush()
    run = ExtractionRun(source_id=src.id); db.add(run); await db.commit()

    blocked_url = "https://www.dell.com/support/manuals/x/preface?lang=en-us"
    ok_url = "https://www.dell.com/support/manuals/x/intro?lang=en-us"

    async def fake_warmup_render(url, selector=None, warmup_url=None, client=None, auth_state=None):
        if url == blocked_url:
            # Akamai shell: empty content selector, block text in the body.
            return {"innerHtml": "", "outerHtml": "", "title": "", "bodyText": _AKAMAI}
        return {"innerHtml": "<p>Real article body here.</p>", "outerHtml": "",
                "title": "Intro", "bodyText": "Real article body here."}

    monkeypatch.setattr(browserless_mod.browserless_client, "warmup_render", fake_warmup_render)

    url_to_entry = {
        blocked_url: {"title": "Preface", "topic_key": blocked_url, "toc_entry_id": None, "sort_order": 0},
        ok_url: {"title": "Intro", "topic_key": ok_url, "toc_entry_id": None, "sort_order": 1},
    }
    await firecrawl_service._scrape_via_browserless(
        db, src.id, run.id, url_to_entry,
        content_spec={"selector": "#divTopicContent", "warmup_url": "https://www.dell.com/support/home/en-us"},
    )

    arts = (await db.execute(select(Article).where(Article.source_id == src.id))).scalars().all()
    stored = {a.source_url for a in arts}
    # The real page is stored; the blocked page is NOT stored as an article.
    assert ok_url in stored
    assert blocked_url not in stored

    # The run is flagged blocked and the blocked page is queued for retry.
    run2 = (await db.execute(select(ExtractionRun).where(ExtractionRun.id == run.id))).scalar_one()
    assert run2.error_message == _BLOCKED_MSG
    assert run2.blocked_pending and any(
        it.get("url") == blocked_url for it in run2.blocked_pending
    )
