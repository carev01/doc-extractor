"""Bulk extract: queue an ordinary extraction run for many sources at once.

The product and vendor "Extract" actions preview their sources with each one's
eligibility, then trigger the ones the user kept. Every run is a plain
``extract`` run on the existing queue, so the worker still executes them one at
a time, oldest first — bulk never adds parallel Firecrawl/Browserless load. The
one-source escape hatches (``force``, ``allow_toc_collapse``) are deliberately
not offered in bulk.

A source is skipped, with a reason, exactly where a single trigger would refuse
it: no write access, an already-active run, or an expired login. A source the
caller cannot even read is reported ``not_found`` — vendors outside a caller's
grants are 404-invisible everywhere else, and bulk must not reveal them.
"""
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal
from app.core.sql import any_of
from app.models.auth_realm import AuthRealm, RealmStatus
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.product import Product
from app.models.source import DocumentationSource
from app.services.auth.session import session_expired
from app.services.queue import ActiveRunExists, enqueue_run

ACTIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED)


def realm_blocks_extraction(realm: AuthRealm | None) -> bool:
    """True when *realm*'s session can no longer authenticate a run."""
    return realm is not None and (session_expired(realm) or realm.status == RealmStatus.EXPIRED)


@dataclass
class Candidate:
    source_id: uuid.UUID
    source_name: str
    product_id: uuid.UUID
    product_name: str
    vendor_id: uuid.UUID
    reason: str | None  # None = eligible

    @property
    def eligible(self) -> bool:
        return self.reason is None


async def _candidates(db: AsyncSession, principal: Principal, where) -> list[Candidate]:
    rows = (await db.execute(
        select(DocumentationSource.id, DocumentationSource.name, DocumentationSource.auth_realm_id,
               Product.id.label("product_id"), Product.name.label("product_name"), Product.vendor_id)
        .join(Product, Product.id == DocumentationSource.product_id)
        .where(where)
        .order_by(Product.name, DocumentationSource.name)
    )).all()
    ids = [r.id for r in rows]
    active = set((await db.execute(
        select(ExtractionRun.source_id).where(
            any_of(ExtractionRun.source_id, ids), ExtractionRun.status.in_(ACTIVE_STATUSES))
    )).scalars()) if ids else set()
    realm_ids = {r.auth_realm_id for r in rows if r.auth_realm_id}
    realms = {r.id: r for r in (await db.execute(
        select(AuthRealm).where(any_of(AuthRealm.id, list(realm_ids)))
    )).scalars()} if realm_ids else {}

    out = []
    for r in rows:
        if not principal.can_read_vendor(r.vendor_id):
            reason = "not_found"
        elif not principal.can_write_vendor(r.vendor_id):
            reason = "no_write_access"
        elif r.id in active:
            reason = "active_run"
        elif realm_blocks_extraction(realms.get(r.auth_realm_id)):
            reason = "session_expired"
        else:
            reason = None
        out.append(Candidate(r.id, r.name, r.product_id, r.product_name, r.vendor_id, reason))
    return out


async def preview(db: AsyncSession, principal: Principal, *,
                  vendor_id: uuid.UUID | None = None,
                  product_id: uuid.UUID | None = None) -> list[Candidate]:
    """Every source in the product or vendor, with its eligibility. The caller
    has already authorized read access to that scope."""
    where = (Product.id == product_id) if product_id else (Product.vendor_id == vendor_id)
    return await _candidates(db, principal, where)


async def trigger(db: AsyncSession, principal: Principal,
                  source_ids: Sequence[uuid.UUID]) -> tuple[list[tuple[uuid.UUID, uuid.UUID]], list[tuple[uuid.UUID, str]]]:
    """Queue a run for each eligible source; returns (queued [(source, run)], skipped [(source, reason)]).

    Eligibility is re-checked here — a run may have started since the preview —
    and ``enqueue_run``'s own active-run constraint is the final word, so a
    source can never get a second active run. Works from plain ids: each enqueue
    commits (and rolls back on a conflict), which expires ORM instances."""
    wanted = list(dict.fromkeys(source_ids))  # de-duplicated, order kept
    found = {c.source_id: c for c in await _candidates(db, principal, any_of(DocumentationSource.id, wanted))}
    queued: list[tuple[uuid.UUID, uuid.UUID]] = []
    skipped: list[tuple[uuid.UUID, str]] = []
    for sid in wanted:
        c = found.get(sid)
        reason = "not_found" if c is None else c.reason
        if reason:
            skipped.append((sid, reason))
            continue
        try:
            run = await enqueue_run(db, sid, trigger="bulk")
        except ActiveRunExists:
            skipped.append((sid, "active_run"))
            continue
        queued.append((sid, run.id))
    return queued, skipped
