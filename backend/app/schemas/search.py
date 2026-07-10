"""Pydantic schemas for enhanced article search/filtering."""

import base64
import json
import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ChangeStatus(str, Enum):
    """Change-status of an article relative to the latest completed run.

    Used both as the ``status`` query-param filter (FastAPI validates the value
    and returns 422 for anything else) and as the per-item ``change_status``.
    """
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class FacetCount(BaseModel):
    """A single facet bucket — label + count."""
    label: str
    count: int


class Facets(BaseModel):
    """Facet counts scoped to the current filter set (excluding the status
    dimension, so the status counts reflect the full unfiltered-by-status
    breakdown the user could drill into)."""
    status: list[FacetCount] = []
    date_bucket: list[FacetCount] = []


class ArticleSearchResultItem(BaseModel):
    """A single article in search results.

    A superset of ``ArticleResponse`` — every field the legacy list endpoint
    returned, plus the additive ``search_rank`` (non-null only when ``q`` is
    active) and ``change_status``."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    toc_entry_id: uuid.UUID | None
    title: str
    source_url: str
    last_updated_at: datetime | None
    sort_order: int
    estimated_tokens: int
    content_size_bytes: int
    created_at: datetime
    extracted_at: datetime
    search_rank: float | None = None
    change_status: ChangeStatus | None = None


class ArticleSearchResponse(BaseModel):
    """Paginated search response with cursor metadata and facets.

    Backward compatible with the legacy ``ArticleListResponse`` shape: the
    ``articles`` list and ``total`` are still present (``total`` is the full
    COUNT on the first page; it is ``None`` on cursor-continuation pages, where
    it is neither needed nor cheap to recompute). New fields ``next_cursor``,
    ``has_more``, ``limit`` and ``facets`` are additive.
    """
    articles: list[ArticleSearchResultItem]
    total: int | None = None
    # Cursor pagination — ``next_cursor`` is populated whenever ``has_more`` is
    # true, in every mode (including the default first page), so a client can
    # always page forward by echoing it back as ``?cursor=``.
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    # Faceted counts — computed once, on the first page only.
    facets: Facets | None = None


# ── Cursor encoding/decoding ──────────────────────────────────────────────
#
# The cursor is an opaque, URL-safe base64 of a small JSON payload. Its shape
# depends on the active ordering:
#   • browse (no ``q``): keyset on (sort_order, id) → {"o": <int>, "id": <uuid>}
#   • search (``q`` set): stable offset over the rank ordering → {"off": <int>}
# The route interprets the payload; clients treat it as opaque.


def encode_cursor(payload: dict) -> str:
    """Encode a cursor payload as a URL-safe base64 JSON token."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict:
    """Decode a cursor token to its payload dict.

    Raises ``ValueError`` if the token is malformed (caller maps this to 422).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid cursor token") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid cursor payload")
    return payload
