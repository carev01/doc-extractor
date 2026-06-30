"""PDF extraction honors a cancel signal (raises RunControlSignal before doing
the heavy work) instead of running to completion / being reaped as 'worker lost'."""

import os
import sys
import types
import uuid

import fitz
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource
from app.models.extraction_run import ExtractionRun, RunStatus
from app.services import pdf_import
from app.services.firecrawl import RunControlSignal

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


def _tiny_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "x")
    data = doc.tobytes()
    doc.close()
    return data


async def test_run_pdf_extraction_honors_cancel(factory, monkeypatch):
    async with factory() as s:
        v = Vendor(name="V"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="Doc",
                                  base_url="https://x/d.pdf", source_type="pdf")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status=RunStatus.RUNNING)
        s.add(run); await s.commit()
        src_id, run_id = src.id, run.id

    async def fake_acquire(source, auth_cookies=None):
        return _tiny_pdf(), "deadbeef"

    monkeypatch.setattr(pdf_import, "acquire_pdf", fake_acquire)

    convert_called = {"v": False}

    async def fake_convert(*a, **kw):
        convert_called["v"] = True
        return types.SimpleNamespace(engine="docling")

    monkeypatch.setattr(pdf_import, "convert_pdf", fake_convert)

    # Service stub whose control check fires a cancel.
    async def raise_cancel(db, run_pk):
        raise RunControlSignal("cancel")

    service = types.SimpleNamespace(_raise_if_controlled=raise_cancel)

    async with factory() as db:
        src = await db.get(DocumentationSource, src_id)
        run = await db.get(ExtractionRun, run_id)
        with pytest.raises(RunControlSignal) as ei:
            await pdf_import.run_pdf_extraction(service, db, src, run, run_id)
        assert ei.value.action == "cancel"

    # The cancel check gates the heavy convert — it must not have run.
    assert convert_called["v"] is False
