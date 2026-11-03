"""An authenticated source whose session died before TOC discovery.

AvePoint Learn answers a request carrying an *expired* session cookie with a
200 "Redirecting…" shell (a meta refresh to its SSO endpoint), so the rspress
profile finds no sidebar and returns an empty TOC. Nothing in the TOC phase
looked for an auth wall — only the content phase did — so on 2026-11-01 every
AvePoint run of a long job firing either aborted on the TOC-collapse guard or,
for a guide below the guard's floor, "completed" with the synthetic Index page
and soft-removed the whole guide (MyHub Administrator Guide, 11 articles).

An empty TOC on an authenticated source now probes the root page; a wall is
handled exactly like a mid-run one (re-login once for a job run, else PAUSE),
before anything is persisted or removed. No network: fetch_raw is stubbed.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core import crypto
from app.core.config import settings
from app.core.database import Base
from tests.db_reset import reset_schema
from app.models import Article, AuthRealm, DocumentationSource, ExtractionRun, Product, Vendor
from app.models.auth_realm import RealmStatus
from app.models.extraction_run import RunStatus
from app.models.toc import TOCEntry
from app.services import firecrawl as fc_mod
from app.services.firecrawl import FirecrawlService, TocCollapseError

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio

ROOT = "https://learn.avepoint.com/myhub-administrator-guide/about-myhub.html"
SSO_SHELL = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Redirecting…</title></head>'
    '<body data-redirect-url="/api/auth/aos-sso/COMMERCIAL?returnUrl=%2Fx">'
    '<noscript><meta http-equiv="refresh" content="0;url=/api/auth/aos-sso/COMMERCIAL?returnUrl=%2Fx">'
    '</noscript></body></html>'
)


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(reset_schema)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    crypto._reset_cache()


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    async def _quiet(*a, **kw):
        return None
    monkeypatch.setattr(fc_mod, "notify", _quiet)


class _EmptyTocProfile:
    name = "stub"
    content_engine = "raw_http"
    render_engine = None

    def content_config(self):
        return {}

    async def build_toc(self, root_url, scraper):
        return []


def _service(monkeypatch, factory, root_html: str) -> tuple[FirecrawlService, list]:
    svc = FirecrawlService()
    monkeypatch.setattr(fc_mod, "async_session", factory)
    fetched: list = []

    async def available():
        return True

    async def resolve(source, auth_cookies=None):
        return _EmptyTocProfile()

    async def fetch_raw(url, cookies=None, **kw):
        fetched.append((url, bool(cookies)))
        return root_html

    async def no_scrape(*a, **kw):
        return None

    monkeypatch.setattr(svc, "_check_available", available)
    monkeypatch.setattr(svc, "_resolve_profile", resolve)
    monkeypatch.setattr(svc, "fetch_raw", fetch_raw)
    monkeypatch.setattr(svc, "_scrape_via_raw_http", no_scrape)
    return svc, fetched


async def _source(factory, *, live: int):
    """An authenticated source with ``live`` articles — below the guard's
    min_prior when small, which is the case that used to wipe the guide."""
    exp = (datetime.now(timezone.utc) + timedelta(days=1)).timestamp()
    async with factory() as s:
        v = Vendor(name="AvePoint"); s.add(v); await s.flush()
        p = Product(name="MyHub", vendor_id=v.id); s.add(p); await s.flush()
        r = AuthRealm(
            name="AvePoint Learn", login_domain="learn.avepoint.com",
            browserless_profile_name="realm-a", status=RealmStatus.ACTIVE,
            state_snapshot={"cookies": [{"name": "session_token", "value": "v",
                                         "expires": exp}]},
        )
        s.add(r); await s.flush()
        src = DocumentationSource(name="Administrator Guide", base_url=ROOT,
                                  product_id=p.id, platform="rspress", auth_realm_id=r.id)
        s.add(src); await s.flush()
        for i in range(live):
            s.add(Article(source_id=src.id, title=f"P{i}", topic_key=f"k{i}",
                          source_url=f"{ROOT}#{i}", content_markdown="body"))
        run = ExtractionRun(source_id=src.id, status=RunStatus.PENDING)
        s.add(run)
        await s.commit()
        return src.id, r.id, run.id


async def test_empty_toc_behind_an_sso_shell_pauses_without_removing(factory, monkeypatch):
    src_id, realm_id, run_id = await _source(factory, live=11)
    svc, fetched = _service(monkeypatch, factory, SSO_SHELL)

    async with factory() as s:
        run = await svc.extract_source(s, src_id, run_id=run_id)

    assert fetched == [(ROOT, True)]          # the root, with the session cookie
    assert run.status == RunStatus.PAUSED     # manual run: no re-login, pause
    async with factory() as s:
        realm = await s.get(AuthRealm, realm_id)
        assert realm.status == RealmStatus.EXPIRED
        live = (await s.execute(select(func.count()).select_from(Article).where(
            Article.source_id == src_id, Article.removed_at.is_(None)))).scalar()
        assert live == 11
        toc = (await s.execute(select(func.count()).select_from(TOCEntry).where(
            TOCEntry.source_id == src_id))).scalar()
        assert toc == 0                        # no synthetic Index persisted


async def test_empty_toc_without_a_wall_still_hits_the_guard(factory, monkeypatch):
    # A real page that merely has no nav: not a wall, so the collapse guard
    # (now covering small sources whose TOC fell to one page) aborts instead.
    src_id, _, run_id = await _source(factory, live=11)
    svc, fetched = _service(monkeypatch, factory, "<html><body><p>Docs</p></body></html>")

    async with factory() as s:
        with pytest.raises(TocCollapseError):
            await svc.extract_source(s, src_id, run_id=run_id)
    assert len(fetched) == 1
