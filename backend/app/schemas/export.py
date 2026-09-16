"""Pydantic schemas for export requests."""

import uuid

from pydantic import BaseModel


class ExportJobCreatedResponse(BaseModel):
    export_job_id: uuid.UUID
    status: str


class ExportJobStatusResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    status: str
    export_id: uuid.UUID | None
    zip_filename: str | None
    files: list["ExportFileInfo"] | None
    error_message: str | None


class ExportRequest(BaseModel):
    """Request to export documentation as markdown."""

    source_id: uuid.UUID
    # Selection: none = full export
    article_ids: list[uuid.UUID] | None = None  # specific articles
    toc_entry_ids: list[uuid.UUID] | None = None  # specific chapters/sections
    topic_query: str | None = None  # full-text search within content

    # Splitting options
    split_by: str | None = None  # "size" | "articles" | "tokens" | None (no split)
    max_articles_per_file: int | None = None
    max_file_size_bytes: int | None = None
    max_tokens_per_file: int | None = None
    # Align file boundaries to chapters (top-level TOC). Trades uniform file
    # sizes for chapter coherence — files may come out smaller.
    respect_chapters: bool = False
    # Output format. "pdf" renders each group to a self-contained PDF.
    format: str = "markdown"  # "markdown" | "pdf"
    # Markdown only: when True, images are bundled alongside the .md file(s) in a
    # zip. Defaults to OFF: image payloads dwarf the text (Veeam Backup &
    # Replication is 27 MB of markdown against 495 MB of images) and most
    # consumers of an export want the text, so opting in is the honest default —
    # opting out was something you only discovered after a 500 MB download.
    # Ignored for PDF (images are embedded in the PDF itself).
    include_images: bool = False


class ExportResponse(BaseModel):
    export_id: uuid.UUID
    source_id: uuid.UUID
    file_count: int
    total_articles: int
    total_size_bytes: int
    zip_filename: str | None = None  # markdown/image bundle; None for PDF (self-contained)
    files: list["ExportFileInfo"]


class ExportFileInfo(BaseModel):
    filename: str
    article_count: int
    size_bytes: int
    estimated_tokens: int
    first_article_title: str
    last_article_title: str


class ExtractionTriggerResponse(BaseModel):
    run_id: uuid.UUID
    source_id: uuid.UUID
    status: str
    message: str


# ---------------------------------------------------------------------------
# Product-level (batch) export
# ---------------------------------------------------------------------------


class ProductExportRequest(BaseModel):
    """Export every source of a product, as one job per source.

    Deliberately a *subset* of ExportRequest. ``article_ids`` and
    ``toc_entry_ids`` identify rows within a single source, so they have no
    meaning across a product and are omitted rather than silently ignored.
    ``topic_query`` does generalise — it is applied within each source — and a
    source it matches nothing in is skipped rather than failing the batch.
    """

    split_by: str | None = None
    max_articles_per_file: int | None = None
    max_file_size_bytes: int | None = None
    max_tokens_per_file: int | None = None
    respect_chapters: bool = False
    format: str = "markdown"
    include_images: bool = False
    topic_query: str | None = None
    # Restrict to a subset of the product's sources; None/empty = all of them.
    source_ids: list[uuid.UUID] | None = None


class BatchSourceStatus(BaseModel):
    job_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    seq: int
    status: str
    export_id: uuid.UUID | None = None
    article_count: int | None = None
    size_bytes: int | None = None
    error_message: str | None = None


class ExportBatchResponse(BaseModel):
    batch_id: uuid.UUID
    label: str
    total: int
    completed: int
    failed: int
    pending: int
    running: int
    cancelled: int
    # True once nothing is pending or running — the download can be built even if
    # some sources failed, from whichever ones completed.
    finished: bool
    total_size_bytes: int
    sources: list[BatchSourceStatus]


class ProductExportCreatedResponse(BaseModel):
    batch_id: uuid.UUID
    total: int
    # Sources left out because they hold no articles yet: exporting them would
    # raise "No articles matched" and show as a failure the operator can't act on.
    skipped: list[str] = []


class ProductExportPreview(BaseModel):
    """Projected size of a product export, so the operator sees what they're
    about to generate before committing the worker to it."""

    product_id: uuid.UUID
    label: str
    source_count: int
    exportable_source_count: int
    skipped: list[str] = []
    total_articles: int
    markdown_bytes: int
    image_bytes: int
    # markdown_bytes + (image_bytes when images are included)
    projected_bytes: int
