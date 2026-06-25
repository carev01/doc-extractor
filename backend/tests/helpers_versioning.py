"""Shared async helpers for versioning/topic_key tests.

Provides ``make_service_and_source`` to build a Vendor→Product→Source fixture
and ``_make_run`` to create a RUNNING ExtractionRun for that source.
Modelled on the async-session + factory pattern used in ``tests/test_versions.py``.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Vendor, Product, DocumentationSource, ExtractionRun
from app.models.extraction_run import RunStatus
from app.services.firecrawl import FirecrawlService


async def make_service_and_source(
    db: AsyncSession,
    url_template: str,
    version: str,
) -> tuple["FirecrawlService", "DocumentationSource"]:
    """Insert Vendor → Product(version) → Source(url_template) and return
    (FirecrawlService instance, source).  The caller is responsible for the
    session lifecycle (flush / commit as needed).
    """
    vendor = Vendor(name=f"TestVendor-{version}")
    db.add(vendor)
    await db.flush()

    product = Product(vendor_id=vendor.id, name=f"TestProduct-{version}", version=version)
    db.add(product)
    await db.flush()

    base_url = url_template.replace("{version}", version)
    source = DocumentationSource(
        product_id=product.id,
        name=f"TestSource-{version}",
        base_url=base_url,
        url_template=url_template,
    )
    db.add(source)
    await db.flush()

    svc = FirecrawlService()
    return svc, source


async def _make_run(db: AsyncSession, source: "DocumentationSource") -> "ExtractionRun":
    """Insert a PENDING ExtractionRun for *source* and return it.

    Transitions any existing active (PENDING/RUNNING) run for this source to
    COMPLETED first, since the unique index ``uq_active_run_per_source`` allows
    only one active run per source at a time.
    """
    await db.execute(
        update(ExtractionRun)
        .where(
            ExtractionRun.source_id == source.id,
            ExtractionRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING]),
        )
        .values(status=RunStatus.COMPLETED)
    )
    await db.flush()
    run = ExtractionRun(source_id=source.id, status=RunStatus.PENDING)
    db.add(run)
    await db.flush()
    return run
