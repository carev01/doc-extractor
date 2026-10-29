"""Tier routes — operator-defined vendor labels that jobs can target (admin-only)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal, require_admin
from app.core.database import get_db
from app.core.sql import any_of
from app.models import Job, Tier, Vendor
from app.schemas.tier import TierCreate, TierResponse, TierUpdate, TierVendorsSet


def _require_admin(principal: Principal = Depends(get_principal)) -> None:
    require_admin(principal)


router = APIRouter(prefix="/api/tiers", tags=["tiers"], dependencies=[Depends(_require_admin)])


async def _counts(db: AsyncSession, tier_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    out = {t: [0, 0] for t in tier_ids}
    if not out:
        return {}
    for tid, n in (await db.execute(
        select(Vendor.tier_id, func.count()).where(any_of(Vendor.tier_id, tier_ids))
        .group_by(Vendor.tier_id)
    )).all():
        out[tid][0] = n
    for tid, n in (await db.execute(
        select(Job.tier_id, func.count()).where(any_of(Job.tier_id, tier_ids))
        .group_by(Job.tier_id)
    )).all():
        out[tid][1] = n
    return {k: (v[0], v[1]) for k, v in out.items()}


def _resp(t: Tier, counts: dict) -> TierResponse:
    v, j = counts.get(t.id, (0, 0))
    return TierResponse(id=t.id, name=t.name, sort_order=t.sort_order,
                        vendor_count=v, job_count=j)


async def _load(db: AsyncSession, tier_id: uuid.UUID) -> Tier:
    tier = await db.get(Tier, tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    return tier


_NAME_TAKEN = "A tier with that name already exists"
_IN_USE_NAMES_CAP = 20


async def _commit_name(db: AsyncSession) -> None:
    """Commit a tier name change. The explicit ``_name_taken`` check is
    check-then-insert, so a concurrent create can still win the race; the unique
    constraint then surfaces as the same 409, never a 500."""
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=_NAME_TAKEN) from None


async def _name_taken(db: AsyncSession, name: str, exclude: uuid.UUID | None = None) -> bool:
    q = select(Tier.id).where(Tier.name == name)
    if exclude is not None:
        q = q.where(Tier.id != exclude)
    return (await db.execute(q)).first() is not None


@router.get("", response_model=list[TierResponse])
async def list_tiers(db: AsyncSession = Depends(get_db)):
    tiers = (await db.execute(select(Tier).order_by(Tier.sort_order, Tier.name))).scalars().all()
    counts = await _counts(db, [t.id for t in tiers])
    return [_resp(t, counts) for t in tiers]


@router.post("", response_model=TierResponse, status_code=201)
async def create_tier(body: TierCreate, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Tier name is required")
    if await _name_taken(db, name):
        raise HTTPException(status_code=409, detail=_NAME_TAKEN)
    sort_order = body.sort_order
    if sort_order is None:
        sort_order = ((await db.execute(select(func.max(Tier.sort_order)))).scalar() or 0) + 1
    tier = Tier(name=name, sort_order=sort_order)
    db.add(tier)
    await _commit_name(db)
    return _resp(tier, {})


@router.patch("/{tier_id}", response_model=TierResponse)
async def update_tier(tier_id: uuid.UUID, body: TierUpdate, db: AsyncSession = Depends(get_db)):
    tier = await _load(db, tier_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("name") is not None:
        name = data["name"].strip()
        if not name:
            raise HTTPException(status_code=422, detail="Tier name is required")
        if await _name_taken(db, name, exclude=tier.id):
            raise HTTPException(status_code=409, detail=_NAME_TAKEN)
        tier.name = name
    if data.get("sort_order") is not None:
        tier.sort_order = data["sort_order"]
    await _commit_name(db)
    return _resp(tier, await _counts(db, [tier.id]))


@router.delete("/{tier_id}", status_code=204, responses={409: {"description": "Tier in use"}})
async def delete_tier(tier_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    tier = await _load(db, tier_id)
    v, j = (await _counts(db, [tier.id]))[tier.id]
    if v or j:
        # Name who is using it (capped) so the operator knows what to reassign.
        vendor_names = (await db.execute(
            select(Vendor.name).where(Vendor.tier_id == tier.id)
            .order_by(Vendor.name).limit(_IN_USE_NAMES_CAP)
        )).scalars().all()
        job_names = (await db.execute(
            select(Job.name).where(Job.tier_id == tier.id)
            .order_by(Job.name).limit(_IN_USE_NAMES_CAP)
        )).scalars().all()
        return JSONResponse(status_code=409, content={
            "detail": f"Tier is in use by {v} vendor(s) and {j} job(s); "
                      "reassign them before deleting",
            "vendors": v, "jobs": j,
            "vendor_names": list(vendor_names), "job_names": list(job_names),
        })
    try:
        await db.delete(tier)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Tier is in use") from None
    return None


async def _set_vendors(db: AsyncSession, tier_id: uuid.UUID | None, vendor_ids: list[uuid.UUID]):
    ids = list(dict.fromkeys(vendor_ids))
    if ids:
        found = set((await db.execute(select(Vendor.id).where(any_of(Vendor.id, ids)))).scalars())
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise HTTPException(status_code=404, detail=f"Unknown vendor ids: {', '.join(missing)}")
        await db.execute(update(Vendor).where(any_of(Vendor.id, ids)).values(tier_id=tier_id))
    await db.commit()
    return {"tier_id": tier_id, "updated": len(ids)}


# Literal path declared before the {tier_id} one so "none" is not parsed as a UUID.
@router.put("/none/vendors")
async def clear_vendors_tier(body: TierVendorsSet, db: AsyncSession = Depends(get_db)):
    """Remove the given vendors from whatever tier they are in."""
    return await _set_vendors(db, None, body.vendor_ids)


@router.put("/{tier_id}/vendors")
async def set_tier_vendors(
    tier_id: uuid.UUID, body: TierVendorsSet, db: AsyncSession = Depends(get_db)
):
    """Move the given vendors into this tier (other vendors are unchanged)."""
    await _load(db, tier_id)
    return await _set_vendors(db, tier_id, body.vendor_ids)
