"""Cross-source schedule listing for the Jobs view.

Per-source schedule CRUD lives under /api/sources/{id}/schedule; this router
adds a single read endpoint that lists every schedule with its vendor/product/
source names so the Jobs view can show upcoming scheduled tasks in one place.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.product import Product
from app.models.schedule import Schedule
from app.models.source import DocumentationSource
from app.models.vendor import Vendor

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


@router.get("")
async def list_schedules(
    enabled_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """List all schedules (with vendor/product/source names), soonest first."""
    query = (
        select(
            Schedule,
            DocumentationSource.name.label("source_name"),
            Product.name.label("product_name"),
            Vendor.name.label("vendor_name"),
        )
        .join(DocumentationSource, Schedule.source_id == DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .join(Vendor, Product.vendor_id == Vendor.id)
        .order_by(Schedule.next_run_at.asc().nulls_last())
    )
    if enabled_only:
        query = query.where(Schedule.enabled.is_(True))

    rows = (await db.execute(query)).all()
    return {
        "schedules": [
            {
                "source_id": s.source_id,
                "source_name": source_name,
                "product_name": product_name,
                "vendor_name": vendor_name,
                "enabled": s.enabled,
                "frequency": s.frequency,
                "time_of_day": s.time_of_day,
                "cron": s.cron,
                "timezone": s.timezone,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            }
            for s, source_name, product_name, vendor_name in rows
        ]
    }
