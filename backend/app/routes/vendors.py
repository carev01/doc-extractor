"""Vendor CRUD routes.

Vendors are the top of the ownership tree, so they anchor per-vendor
authorization. Creating/updating/deleting vendors is admin-only (this also
prevents a non-admin from re-adding a vendor that's merely hidden from them —
duplicates are additionally blocked by the unique name constraint). Non-admins
only see vendors they hold a grant for.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal, require_admin, require_vendor_read
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
async def create_vendor(
    body: VendorCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Create a new vendor (admin only)."""
    require_admin(principal)
    vendor = Vendor(name=body.name, website=body.website)
    db.add(vendor)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A vendor with that name already exists")
    await db.refresh(vendor)
    return vendor


@router.get("", response_model=VendorListResponse)
async def list_vendors(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """List vendors visible to the caller (admins: all)."""
    visible = principal.visible_vendor_ids()  # None = all
    base = select(Vendor)
    count_q = select(func.count(Vendor.id))
    if visible is not None:
        if not visible:
            return VendorListResponse(vendors=[], total=0)
        base = base.where(Vendor.id.in_(visible))
        count_q = count_q.where(Vendor.id.in_(visible))

    total = (await db.execute(count_q)).scalar()
    result = await db.execute(base.order_by(Vendor.name).offset(skip).limit(limit))
    return VendorListResponse(vendors=result.scalars().all(), total=total)


@router.get("/{vendor_id}", response_model=VendorResponse)
async def get_vendor(
    vendor_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Get a vendor by ID (404 if not visible to the caller)."""
    require_vendor_read(principal, vendor_id)
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.patch("/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: uuid.UUID,
    body: VendorUpdate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Update a vendor (admin only)."""
    require_admin(principal)
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if body.name is not None:
        vendor.name = body.name
    if body.website is not None:
        vendor.website = body.website
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A vendor with that name already exists")
    await db.refresh(vendor)
    return vendor


@router.delete("/{vendor_id}", status_code=204)
async def delete_vendor(
    vendor_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
):
    """Delete a vendor and all associated data (admin only)."""
    require_admin(principal)
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    await db.delete(vendor)
    await db.commit()
