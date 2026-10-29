"""Which sources a job covers, and the inverse (normalizing a picked set).

Coverage is computed, never stored per source: a job covers the sources of its
tier's vendors plus its explicit vendor / product / source targets, so sources
added later are picked up without touching the job.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sql import any_of
from app.models import DocumentationSource, Job, JobTarget, Product, Vendor


@dataclass
class Targets:
    vendor_ids: list[UUID] = field(default_factory=list)
    product_ids: list[UUID] = field(default_factory=list)
    source_ids: list[UUID] = field(default_factory=list)


async def resolve_sources(db: AsyncSession, job_ids: Sequence[UUID]) -> dict[UUID, set[UUID]]:
    """Job id -> covered source ids. One query per target kind, not per job."""
    out: dict[UUID, set[UUID]] = {jid: set() for jid in job_ids}
    if not out:
        return out
    ids = list(out)
    S, P = DocumentationSource, Product

    # Tier: every vendor currently in the job's tier.
    for jid, sid in (await db.execute(
        select(Job.id, S.id)
        .join(Vendor, Vendor.tier_id == Job.tier_id)
        .join(P, P.vendor_id == Vendor.id)
        .join(S, S.product_id == P.id)
        .where(any_of(Job.id, ids))
    )).all():
        out[jid].add(sid)

    # Vendor targets.
    for jid, sid in (await db.execute(
        select(JobTarget.job_id, S.id)
        .join(P, P.vendor_id == JobTarget.vendor_id)
        .join(S, S.product_id == P.id)
        .where(any_of(JobTarget.job_id, ids))
    )).all():
        out[jid].add(sid)

    # Product targets.
    for jid, sid in (await db.execute(
        select(JobTarget.job_id, S.id)
        .join(S, S.product_id == JobTarget.product_id)
        .where(any_of(JobTarget.job_id, ids))
    )).all():
        out[jid].add(sid)

    # Source targets (join so a dangling id could never be reported).
    for jid, sid in (await db.execute(
        select(JobTarget.job_id, S.id)
        .join(S, S.id == JobTarget.source_id)
        .where(any_of(JobTarget.job_id, ids))
    )).all():
        out[jid].add(sid)
    return out


async def normalize_targets(db: AsyncSession, source_ids: Sequence[UUID]) -> Targets:
    """Collapse a picked source set into the fewest targets.

    A product whose current sources are all picked becomes a product target; a
    vendor whose products-with-sources are all fully picked becomes a vendor target.
    Products with no sources are ignored for vendor fullness (the picker gives them
    no checkbox) but never become product targets themselves.
    """
    picked = set(source_ids)
    if not picked:
        return Targets()
    S, P = DocumentationSource, Product

    # Candidate products/vendors are those touched by the pick.
    touched = (await db.execute(
        select(S.product_id, P.vendor_id)
        .join(P, P.id == S.product_id)
        .where(any_of(S.id, picked))
    )).all()
    product_ids = {r[0] for r in touched}
    vendor_ids = {r[1] for r in touched}

    prod_sources: dict[UUID, set[UUID]] = defaultdict(set)
    for pid, sid in (await db.execute(
        select(S.product_id, S.id).where(any_of(S.product_id, product_ids))
    )).all():
        prod_sources[pid].add(sid)
    full_products = {p for p, ss in prod_sources.items() if ss and ss <= picked}

    vendor_products: dict[UUID, set[UUID]] = defaultdict(set)
    for vid, pid in (await db.execute(
        select(P.vendor_id, P.id)
        .join(S, S.product_id == P.id)
        .where(any_of(P.vendor_id, vendor_ids))
        .distinct()
    )).all():
        vendor_products[vid].add(pid)
    full_vendors = {v for v, ps in vendor_products.items() if ps and ps <= full_products}

    prod_vendor = {r[0]: r[1] for r in touched}
    targets = Targets(vendor_ids=sorted(full_vendors, key=str))
    targets.product_ids = sorted(
        (p for p in full_products if prod_vendor[p] not in full_vendors), key=str
    )
    covered = set(full_products)
    targets.source_ids = sorted(
        (sid for sid in picked
         if not any(sid in prod_sources[p] for p in covered)),
        key=str,
    )
    return targets
