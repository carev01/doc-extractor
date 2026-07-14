"""Backoff-and-retry for transient PDF download failures.

A retryable PdfAcquireError requeues the run with an exponential delay
(next_attempt_at) instead of failing it, so a source whose download flaked (e.g.
Dell's CDN) is retried later without blocking the queue — until the attempt cap.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, ExtractionRun
from app.models.extraction_run import RunStatus
from app.models.source import SourceStatus
from app.services.pdf_import import PdfAcquireError
from app.services.queue import claim_next_run, retry_delay_seconds
import app.worker as worker

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def test_retry_delay_is_exponential_and_capped():
    base = settings.pdf_download_retry_base_seconds
    assert retry_delay_seconds(1) == base
    assert retry_delay_seconds(2) == base * 2
    assert retry_delay_seconds(3) == base * 4
    # Huge attempt count is clamped to the cap, never overflows.
    assert retry_delay_seconds(999) == settings.pdf_download_retry_max_seconds


async def _mk_source(factory) -> uuid.UUID:
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(
            product_id=p.id, name="M", base_url="file://x.pdf",
            source_type="pdf", status=SourceStatus.EXTRACTING,
        )
        s.add(src); await s.commit()
        return src.id


async def test_run_with_future_next_attempt_is_not_claimed(factory):
    sid = await _mk_source(factory)
    async with factory() as s:
        s.add(ExtractionRun(
            source_id=sid, status=RunStatus.PENDING,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        await s.commit()
    async with factory() as s:
        assert await claim_next_run(s, "w1") is None       # backoff not elapsed


async def test_run_with_elapsed_next_attempt_is_claimed(factory):
    sid = await _mk_source(factory)
    async with factory() as s:
        s.add(ExtractionRun(
            source_id=sid, status=RunStatus.PENDING,
            next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
        await s.commit()
    async with factory() as s:
        claimed = await claim_next_run(s, "w1")
        assert claimed is not None
        assert claimed.status == RunStatus.RUNNING
        assert claimed.next_attempt_at is None              # cleared on claim


async def test_retryable_pdf_failure_requeues_with_backoff(factory, monkeypatch):
    sid = await _mk_source(factory)
    async with factory() as s:
        run = ExtractionRun(source_id=sid, status=RunStatus.PENDING)
        s.add(run); await s.commit(); rid = run.id

    async def boom(db, source_id, run_id=None):
        raise PdfAcquireError("CDN flaked", retryable=True)

    monkeypatch.setattr(worker.firecrawl_service, "extract_source", boom)
    assert await worker.run_one(claim_session_factory=factory, work_session_factory=factory)

    async with factory() as s:
        r = await s.get(ExtractionRun, rid)
        assert r.status == RunStatus.PENDING          # requeued, not failed
        assert r.attempts == 1
        assert r.next_attempt_at is not None and r.next_attempt_at > datetime.now(timezone.utc)
        assert r.claimed_by is None
        src = await s.get(DocumentationSource, sid)
        assert src.status == SourceStatus.PENDING     # "queued", not failed


async def test_retryable_failure_fails_at_attempt_cap(factory, monkeypatch):
    sid = await _mk_source(factory)
    async with factory() as s:
        # One claim away from the cap: claim_next_run increments to the cap.
        run = ExtractionRun(
            source_id=sid, status=RunStatus.PENDING,
            attempts=settings.pdf_download_max_attempts - 1,
        )
        s.add(run); await s.commit(); rid = run.id

    async def boom(db, source_id, run_id=None):
        raise PdfAcquireError("still down", retryable=True)

    monkeypatch.setattr(worker.firecrawl_service, "extract_source", boom)
    await worker.run_one(claim_session_factory=factory, work_session_factory=factory)

    async with factory() as s:
        r = await s.get(ExtractionRun, rid)
        assert r.status == RunStatus.FAILED
        assert r.attempts == settings.pdf_download_max_attempts
        src = await s.get(DocumentationSource, sid)
        assert src.status == SourceStatus.FAILED


async def test_non_retryable_failure_fails_immediately(factory, monkeypatch):
    sid = await _mk_source(factory)
    async with factory() as s:
        run = ExtractionRun(source_id=sid, status=RunStatus.PENDING)
        s.add(run); await s.commit(); rid = run.id

    async def boom(db, source_id, run_id=None):
        raise PdfAcquireError("missing upload", retryable=False)

    monkeypatch.setattr(worker.firecrawl_service, "extract_source", boom)
    await worker.run_one(claim_session_factory=factory, work_session_factory=factory)

    async with factory() as s:
        r = await s.get(ExtractionRun, rid)
        assert r.status == RunStatus.FAILED           # not retried
        assert r.next_attempt_at is None
