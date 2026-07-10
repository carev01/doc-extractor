"""Row-level (per-vendor) authorization.

Layered on top of the global RBAC enforced by AuthMiddleware:
  - admins (and the "auth disabled" dev mode) are *unrestricted* — full access to
    every vendor;
  - non-admins can only see/act on vendors they hold a grant for
    (``user_vendor_permissions``). No grant ⇒ the vendor and everything under it
    is invisible (404, never 403 — we don't leak existence).

A ``Principal`` snapshots the caller's access for the request; the helpers here
resolve an arbitrary resource to its owning vendor and enforce read/write.
"""

from dataclasses import dataclass, field

import uuid as _uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.article import Article
from app.models.extraction_run import ExtractionRun
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.user import User, UserRole
from app.models.user_vendor_permission import UserVendorPermission, VendorAccessLevel


@dataclass
class Principal:
    """The caller's effective access for one request."""
    unrestricted: bool                 # admin or auth-disabled → sees/does everything
    role: UserRole
    user: User | None = None
    vendor_levels: dict[_uuid.UUID, VendorAccessLevel] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.unrestricted or self.role == UserRole.ADMIN

    def visible_vendor_ids(self) -> "set[_uuid.UUID] | None":
        """Set of readable vendor ids, or None meaning ALL (unrestricted)."""
        return None if self.unrestricted else set(self.vendor_levels)

    def can_read_vendor(self, vendor_id: _uuid.UUID) -> bool:
        return self.unrestricted or vendor_id in self.vendor_levels

    def can_write_vendor(self, vendor_id: _uuid.UUID) -> bool:
        if self.unrestricted:
            return True
        return self.vendor_levels.get(vendor_id) == VendorAccessLevel.READ_WRITE


async def get_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    """Build the request Principal from the user AuthMiddleware authenticated."""
    # Auth disabled (dev) → wide open, mirroring the middleware no-op.
    if not settings.auth_jwt_secret:
        return Principal(unrestricted=True, role=UserRole.ADMIN)

    user = getattr(request.state, "user", None)
    role = getattr(request.state, "effective_role", None)
    if user is None or role is None:
        # Should not happen for a non-exempt route (middleware would have 401'd),
        # but never fail open.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    if role == UserRole.ADMIN:
        return Principal(unrestricted=True, role=role, user=user)

    grants = (
        await db.execute(
            select(UserVendorPermission.vendor_id, UserVendorPermission.level)
            .where(UserVendorPermission.user_id == user.id)
        )
    ).all()
    return Principal(
        unrestricted=False,
        role=role,
        user=user,
        vendor_levels={vid: lvl for vid, lvl in grants},
    )


# ── Enforcement on a known vendor_id ────────────────────────────────────────

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def require_vendor_read(principal: Principal, vendor_id: _uuid.UUID | None) -> None:
    if vendor_id is None or not principal.can_read_vendor(vendor_id):
        raise _NOT_FOUND


def require_vendor_write(principal: Principal, vendor_id: _uuid.UUID | None) -> None:
    if vendor_id is None or not principal.can_read_vendor(vendor_id):
        raise _NOT_FOUND  # invisible → 404, don't reveal it exists
    if not principal.can_write_vendor(vendor_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Read-only access to this vendor")


def require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin")


# ── Resolve a resource to its owning vendor_id ──────────────────────────────

async def vendor_of_product(db: AsyncSession, product_id: _uuid.UUID) -> _uuid.UUID | None:
    return (
        await db.execute(select(Product.vendor_id).where(Product.id == product_id))
    ).scalar_one_or_none()


async def vendor_of_source(db: AsyncSession, source_id: _uuid.UUID) -> _uuid.UUID | None:
    return (
        await db.execute(
            select(Product.vendor_id)
            .join(DocumentationSource, DocumentationSource.product_id == Product.id)
            .where(DocumentationSource.id == source_id)
        )
    ).scalar_one_or_none()


async def vendor_of_run(db: AsyncSession, run_id: _uuid.UUID) -> _uuid.UUID | None:
    return (
        await db.execute(
            select(Product.vendor_id)
            .join(DocumentationSource, DocumentationSource.product_id == Product.id)
            .join(ExtractionRun, ExtractionRun.source_id == DocumentationSource.id)
            .where(ExtractionRun.id == run_id)
        )
    ).scalar_one_or_none()


async def vendor_of_article(db: AsyncSession, article_id: _uuid.UUID) -> _uuid.UUID | None:
    return (
        await db.execute(
            select(Product.vendor_id)
            .join(DocumentationSource, DocumentationSource.product_id == Product.id)
            .join(Article, Article.source_id == DocumentationSource.id)
            .where(Article.id == article_id)
        )
    ).scalar_one_or_none()


# ── Convenience: resolve + enforce in one call (returns the vendor_id) ──────

async def authorize_product(db, principal, product_id, *, write: bool) -> _uuid.UUID:
    vid = await vendor_of_product(db, product_id)
    (require_vendor_write if write else require_vendor_read)(principal, vid)
    return vid


async def authorize_source(db, principal, source_id, *, write: bool) -> _uuid.UUID:
    vid = await vendor_of_source(db, source_id)
    (require_vendor_write if write else require_vendor_read)(principal, vid)
    return vid


async def authorize_run(db, principal, run_id, *, write: bool) -> _uuid.UUID:
    vid = await vendor_of_run(db, run_id)
    (require_vendor_write if write else require_vendor_read)(principal, vid)
    return vid


async def authorize_article(db, principal, article_id, *, write: bool) -> _uuid.UUID:
    vid = await vendor_of_article(db, article_id)
    (require_vendor_write if write else require_vendor_read)(principal, vid)
    return vid
