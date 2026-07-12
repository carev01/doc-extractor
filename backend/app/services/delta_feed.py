"""Streaming JSONL delta feed over the content_changes outbox.

Ordering is by content_changes.id alone. Gap-freeness under concurrent runs is
guaranteed by a "safe ceiling": the lowest id belonging to any still-active run.
Each run commits a ``run_start`` sentinel row before processing any article, so
an active run always has a COMMITTED floor in id space from the moment it is
active — this closes the flush→commit window where a run's first real row is
assigned an id but not yet visible. Because an active run's rows (starting at its
sentinel) all have id >= its floor, every row served with id < ceiling provably
belongs to a terminal run, so serving strictly below the ceiling never skips a
slow or just-started run's not-yet-committed higher ids. Sentinel rows carry no
article and are skipped by the stream.
"""

import hashlib
import json
import uuid
from typing import AsyncIterator

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import Article
from app.models.content_change import ContentChange
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.product import Product
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.models.vendor import Vendor
from app.schemas.delta import encode_delta_cursor

_BATCH = 500
_ACTIVE = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED)


class ChapterResolver:
    """Resolves (parent_title, top_level_title) for an article's TOC entry,
    loading each source's TOC once and caching it for the stream's lifetime."""

    def __init__(self) -> None:
        self._by_source: dict[uuid.UUID, dict] = {}

    async def _load(self, db: AsyncSession, source_id: uuid.UUID) -> dict:
        cached = self._by_source.get(source_id)
        if cached is not None:
            return cached
        rows = (
            await db.execute(
                select(TOCEntry.id, TOCEntry.parent_id, TOCEntry.title).where(
                    TOCEntry.source_id == source_id
                )
            )
        ).all()
        parent_of = {r.id: r.parent_id for r in rows}
        title_of = {r.id: r.title for r in rows}
        entry = {"parent_of": parent_of, "title_of": title_of}
        self._by_source[source_id] = entry
        return entry

    async def resolve(self, db, source_id, toc_entry_id):
        if source_id is None or toc_entry_id is None:
            return None, None
        m = await self._load(db, source_id)
        parent_of, title_of = m["parent_of"], m["title_of"]
        pid = parent_of.get(toc_entry_id)
        parent_title = title_of.get(pid) if pid is not None else None
        root = toc_entry_id
        seen: set[uuid.UUID] = set()
        while parent_of.get(root) is not None and root not in seen:
            seen.add(root)
            root = parent_of[root]
        top_title = title_of.get(root)
        return parent_title, top_title


async def _safe_ceiling(db: AsyncSession) -> int | None:
    """Lowest content_changes.id belonging to a still-active run, or None."""
    stmt = (
        select(func.min(ContentChange.id))
        .join(ExtractionRun, ExtractionRun.id == ContentChange.run_id)
        .where(ExtractionRun.status.in_(_ACTIVE))
    )
    return (await db.execute(stmt)).scalar()


def _visible_sources_subq(visible_vendor_ids):
    return (
        select(DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .where(Product.vendor_id.in_(visible_vendor_ids))
        .scalar_subquery()
    )


def _vendor_sources_subq(vendor_id):
    return (
        select(DocumentationSource.id)
        .join(Product, DocumentationSource.product_id == Product.id)
        .where(Product.vendor_id == vendor_id)
        .scalar_subquery()
    )


def _images_payload(article: Article) -> list[dict]:
    return [
        {
            "url": img.local_path,
            "alt": img.alt_text,
            "description": getattr(img, "description", None),
            "kind": getattr(img, "kind", None),
        }
        for img in sorted(article.images, key=lambda i: i.sort_order)
    ]


async def _content_record(db, resolver, *, seq, change_type, article, vendor_name, product_name, run_id):
    parent_title, top_title = await resolver.resolve(db, article.source_id, article.toc_entry_id)
    return {
        "seq": seq,
        "change_type": change_type,
        "id": str(article.id),
        "topic_key": article.topic_key,
        "source_id": str(article.source_id),
        "vendor": vendor_name,
        "product": product_name,
        "title": article.title,
        "source_url": article.source_url,
        "last_updated_at": article.last_updated_at.isoformat() if article.last_updated_at else None,
        # Hash of the SERVED content so a consumer can detect enrichment updates
        # (caption injection changes content_markdown but not the Article's raw
        # content_hash). Purely a serve-time value; the Article row is untouched.
        "content_hash": hashlib.sha256(article.content_markdown.encode("utf-8")).hexdigest(),
        "estimated_tokens": article.estimated_tokens,
        "parent_chapter": parent_title,
        "top_level_chapter": top_title,
        "sort_order": article.sort_order,
        "run_id": str(run_id) if run_id else None,
        "content_markdown": article.content_markdown,
        "images": _images_payload(article),
    }


def _tombstone_record(change: ContentChange) -> dict:
    return {
        "seq": change.id,
        "change_type": "removed",
        "id": str(change.article_id) if change.article_id else None,
        "topic_key": change.topic_key,
        "source_id": str(change.source_id) if change.source_id else None,
        "removed_at": change.created_at.isoformat() if change.created_at else None,
        "run_id": str(change.run_id) if change.run_id else None,
    }


def _line(obj: dict) -> str:
    return json.dumps(obj, default=str, separators=(",", ":")) + "\n"


async def stream_delta(
    db: AsyncSession, *, since_seq: int, source_id, vendor_id, visible_vendor_ids
) -> AsyncIterator[str]:
    ceiling = await _safe_ceiling(db)
    resolver = ChapterResolver()
    last = since_seq
    count = 0

    while True:
        conds = [ContentChange.id > last]
        if ceiling is not None:
            conds.append(ContentChange.id < ceiling)
        if source_id is not None:
            conds.append(ContentChange.source_id == source_id)
        if vendor_id is not None:
            conds.append(ContentChange.source_id.in_(_vendor_sources_subq(vendor_id)))
        if visible_vendor_ids is not None:
            conds.append(ContentChange.source_id.in_(_visible_sources_subq(visible_vendor_ids)))

        # Outer-join the live article + vendor/product names. Removed rows and
        # rows whose article was hard-deleted have no Article.
        stmt = (
            select(ContentChange, Article, Vendor.name, Product.name)
            .outerjoin(Article, Article.id == ContentChange.article_id)
            .outerjoin(DocumentationSource, DocumentationSource.id == ContentChange.source_id)
            .outerjoin(Product, Product.id == DocumentationSource.product_id)
            .outerjoin(Vendor, Vendor.id == Product.vendor_id)
            .options(selectinload(Article.images))
            .where(*conds)
            .order_by(ContentChange.id)
            .limit(_BATCH)
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            break
        for change, article, vendor_name, product_name in rows:
            last = change.id
            if change.change_type == "removed":
                yield _line(_tombstone_record(change))
                count += 1
            elif change.change_type in ("added", "updated") and article is not None:
                yield _line(await _content_record(
                    db, resolver, seq=change.id, change_type=change.change_type,
                    article=article, vendor_name=vendor_name, product_name=product_name,
                    run_id=change.run_id,
                ))
                count += 1
            # else: run_start sentinel, or added/updated whose article was
            # hard-deleted → skip (advancing `last` past it is safe: it is
            # permanent — below the ceiling, from a terminal run).
        if len(rows) < _BATCH:
            break

    # `last` is the id of the last row examined (initialized to since_seq, so it
    # equals since_seq when no rows were examined). Every examined row is below the
    # safe ceiling and therefore belongs to a terminal run — it is either served or
    # permanently unservable (an added/updated whose article was hard-deleted). So
    # advancing to `last` even when count == 0 is safe and prevents a poller from
    # re-scanning an all-skip range on every poll.
    next_since = encode_delta_cursor(last)
    yield _line({"control": "cursor", "next_since": next_since, "count": count})


async def stream_bootstrap(
    db: AsyncSession, *, source_id, vendor_id, visible_vendor_ids, bootstrap_after=None
) -> AsyncIterator[str]:
    # Watermark for the follow-up delta = current global max outbox id, but never
    # past the safe ceiling — otherwise a change committed below max_seq by a run
    # still active during bootstrap would be skipped by the first delta pull.
    max_seq = (await db.execute(select(func.max(ContentChange.id)))).scalar() or 0
    ceiling = await _safe_ceiling(db)
    if ceiling is not None:
        max_seq = min(max_seq, ceiling - 1)
    cursor = encode_delta_cursor(max_seq)
    # Deliver the watermark up front so a consumer whose stream drops mid-bootstrap
    # can resume (bootstrap_after) while anchoring incremental to THIS watermark —
    # not one recomputed at resume time, which could miss an update to an
    # already-emitted article.
    yield _line({"control": "bootstrap_start", "next_since": cursor})
    resolver = ChapterResolver()
    last_id: uuid.UUID | None = bootstrap_after
    count = 0

    while True:
        conds = [Article.removed_at.is_(None)]
        if source_id is not None:
            conds.append(Article.source_id == source_id)
        if vendor_id is not None:
            conds.append(Article.source_id.in_(_vendor_sources_subq(vendor_id)))
        if visible_vendor_ids is not None:
            conds.append(Article.source_id.in_(_visible_sources_subq(visible_vendor_ids)))
        if last_id is not None:
            conds.append(Article.id > last_id)

        stmt = (
            select(Article, Vendor.name, Product.name)
            .join(DocumentationSource, DocumentationSource.id == Article.source_id)
            .join(Product, Product.id == DocumentationSource.product_id)
            .join(Vendor, Vendor.id == Product.vendor_id)
            .options(selectinload(Article.images))
            .where(*conds)
            .order_by(Article.id)
            .limit(_BATCH)
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            break
        for article, vendor_name, product_name in rows:
            last_id = article.id
            yield _line(await _content_record(
                db, resolver, seq=None, change_type="added",
                article=article, vendor_name=vendor_name, product_name=product_name,
                run_id=article.extraction_run_id,
            ))
            count += 1
        if len(rows) < _BATCH:
            break

    yield _line({"control": "cursor", "next_since": cursor, "count": count})
