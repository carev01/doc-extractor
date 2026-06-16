"""Vendor CRUD routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.vendor import Vendor
from app.schemas.vendor import (
    VendorCreate,
    VendorUpdate,
    VendorResponse,
    VendorListResponse,
)

router = APIRouter(prefix="/api/vendors", tags=["vendors"])


@router.post("", response_model=VendorResponse, status_code=201)
async def create_vendor(body: VendorCreate, db: AsyncSession = Depends(get_db)):
    """Create a new vendor."""
    vendor = Vendor(name=body.name, website=body.website)
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.get("", response_model=VendorListResponse)
async def list_vendors(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List all vendors with pagination."""
    total_result = await db.execute(select(func.count(Vendor.id)))
    total = total_result.scalar()

    result = await db.execute(
        select(Vendor).order_by(Vendor.name).offset(skip).limit(limit)
    )
    vendors = result.scalars().all()

    return VendorListResponse(vendors=vendors, total=total)


@router.get("/{vendor_id}", response_model=VendorResponse)
async def get_vendor(vendor_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a vendor by ID."""
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.patch("/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: uuid.UUID, body: VendorUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a vendor."""
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if body.name is not None:
        vendor.name = body.name
    if body.website is not None:
        vendor.website = body.website

    await db.commit()
    await db.refresh(vendor)
    return vendor


@router.delete("/{vendor_id}", status_code=204)
async def delete_vendor(vendor_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a vendor and all associated data."""
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    await db.delete(vendor)
    await db.commit()
