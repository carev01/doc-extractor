"""Pydantic schemas for enhanced article search/filtering."""

import base64
import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ChangeStatus(str, Enum):
    """Change-status values for filtering articles."""
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class FacetCount(BaseModel):
    """A single facet bucket — label + count."""
    label: str
    count: int


class Facets(BaseModel):
    """Facet counts scoped to the current filter set (excluding the facet's
    own dimension so counts reflect what the user would see if they removed
    that single filter)."""
    status: list[FacetCount] = []
    date_bucket: list[FacetCount] = []


class ArticleSearchResultItem(BaseModel):
    """A single article in search results — includes search_rank when FTS5
    is active, otherwise None."""
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
    change_status: str | None = None


class ArticleSearchResponse(BaseModel):
    """Paginated search response with cursor metadata and facets.

    Backward compatible with the existing ``ArticleListResponse`` shape —
    the ``articles`` field is present and ``total`` is included (computed
    via a COUNT query; cursor pagination uses it only for display, not for
    page navigation).  New fields ``next_cursor``, ``has_more``, ``limit``,
    and ``facets`` are additive.
    """
    articles: list[ArticleSearchResultItem]
    total: int
    # Cursor pagination
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    # Faceted counts
    facets: Facets | None = None


# ── Cursor encoding/decoding ──────────────────────────────────────────────

def encode_cursor(sort_key: str, value: str | int) -> str:
    """Encode a cursor token (base64-encoded ``sort_key:value``).

    The cursor is opaque to the client; we use base64 so it's URL-safe.
    """
    raw = f"{sort_key}:{value}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor token, returning (sort_key, value) as strings.

    Raises ValueError if the cursor is malformed.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("Invalid cursor token") from exc
    if ":" not in raw:
        raise ValueError("Invalid cursor format — expected 'key:value'")
    key, _, value = raw.partition(":")
    return key, value