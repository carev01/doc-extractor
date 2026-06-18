# Async, Bounded-Memory Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move export onto the existing job queue (run by the worker) and rebuild the export engine to use bounded memory (metadata-only planning + per-chunk rendering; per-chapter PDF render + merge), so export scales to thousands of articles.

**Architecture:** A new `export_jobs` table is a second queue the worker drains. The export engine splits into a plan pass (group from stored metadata only) and a render pass (load and render one chunk at a time; PDF chunks merge via `pypdf`). `POST /api/export` enqueues and returns a job id; the UI polls `GET /api/export/jobs/{id}` and downloads when ready.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, WeasyPrint, `pypdf`; React + TypeScript.

## Global Constraints

- New Alembic migration `down_revision` = the current head (the implementer runs `alembic heads` to confirm; this branch stacks on the PDF-export branch whose latest data migration is `d3e4f5a6b7c8`/`e4f5a6b7c8d9` — use whatever `alembic heads` reports).
- `export_jobs` reuses the queue column shape and the `RunStatus`-style lifecycle, but as its own enum `ExportStatus` (`PENDING|RUNNING|COMPLETED|FAILED|CANCELLED`, stored UPPERCASE like the existing enums).
- The worker claims an extraction run first, else an export job; both run under the existing heartbeat task. The scheduler reaper covers both.
- Plan pass loads only `id, title, sort_order, toc_entry_id, content_size_bytes, estimated_tokens` (no `content_markdown`/`content_html`/images). Render pass loads full content for one chunk at a time.
- PDF: per-chunk render + `pypdf` merge into one output file; markdown: write header then append per chunk. Existing output *content* is unchanged vs today.
- Backend queue/worker tests use the sync `db_session` fixture pattern (`tests/test_integration.py`) or the async patterns already in `test_queue`/`test_worker`; route tests use the async client (`test_versions` fixture). Run from `backend/` with `pytest`.
- Frontend verified via `npm run build` + `npm run lint`; no new lint errors.
- Branch `feat/async-export` (stacked on `feat/pdf-export`). Interpreter `python3`.

---

### Task 1: Add `pypdf` dependency

**Files:**
- Modify: `backend/requirements.txt`

- [ ] **Step 1: Add the dep + install**

Append to `backend/requirements.txt`:
```
pypdf==5.1.0
```
Run: `cd backend && pip3 install --break-system-packages pypdf==5.1.0`
Expected: installs.

- [ ] **Step 2: Verify merge works on host**

Run:
```bash
python3 -c "
import weasyprint, pypdf, io
a=weasyprint.HTML(string='<h1>A</h1>').write_pdf()
b=weasyprint.HTML(string='<h1>B</h1>').write_pdf()
w=pypdf.PdfWriter()
for d in (a,b): w.append(pypdf.PdfReader(io.BytesIO(d)))
out=io.BytesIO(); w.write(out)
print('merged bytes:', len(out.getvalue()), 'pages:', len(pypdf.PdfReader(io.BytesIO(out.getvalue())).pages))
"
```
Expected: prints `pages: 2`.

- [ ] **Step 3: Commit**

```bash
git add backend/requirements.txt
git commit -m "build: add pypdf for PDF chapter merging"
```

---

### Task 2: `ExportJob` model, migration, and queue functions

**Files:**
- Create: `backend/app/models/export_job.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/f5a6b7c8d9e0_add_export_jobs.py`
- Modify: `backend/app/services/queue.py`
- Test: `backend/tests/test_export_queue.py`

**Interfaces:**
- Produces: `ExportJob` model; `ExportStatus` enum; `enqueue_export(db, source_id, request: dict) -> ExportJob`; `claim_next_export(db, worker_id) -> ExportJob | None`; `reap_stale_exports(db, max_attempts=3, stale_seconds=300) -> int`.

- [ ] **Step 1: Create the model**

Create `backend/app/models/export_job.py`:
```python
"""ExportJob model — queued export generation, drained by the worker."""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ExportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExportJob(Base):
    __tablename__ = "export_jobs"

    __table_args__ = (
        Index("ix_export_jobs_pending", "created_at", postgresql_where=text("status = 'PENDING'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=False
    )
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[ExportStatus] = mapped_column(SAEnum(ExportStatus), default=ExportStatus.PENDING, nullable=False)

    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    export_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 2: Register the model**

In `backend/app/models/__init__.py`, add `from app.models.export_job import ExportJob, ExportStatus` and add `"ExportJob"`, `"ExportStatus"` to `__all__`.

- [ ] **Step 3: Write the migration**

Run `cd backend && alembic heads` to get the current head; use it as `down_revision`. Create `backend/alembic/versions/f5a6b7c8d9e0_add_export_jobs.py`:
```python
"""add export_jobs

Revision ID: f5a6b7c8d9e0
Revises: <CURRENT_HEAD>
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "<CURRENT_HEAD>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("documentation_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("status", sa.Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", name="exportstatus"), nullable=False),
        sa.Column("claimed_by", sa.String(255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("export_id", UUID(as_uuid=True), nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error_message", sa.String(4096), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_export_jobs_pending", "export_jobs", ["created_at"], postgresql_where=sa.text("status = 'PENDING'"))


def downgrade() -> None:
    op.drop_index("ix_export_jobs_pending", table_name="export_jobs")
    op.drop_table("export_jobs")
    sa.Enum(name="exportstatus").drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 4: Apply the migration**

Run: `cd backend && alembic upgrade head`
Expected: completes; `export_jobs` table exists.

- [ ] **Step 5: Write the failing queue tests**

Create `backend/tests/test_export_queue.py` (mirror `test_queue.py`'s NullPool async fixture):
```python
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, ExportJob
from app.models.export_job import ExportStatus
from app.services.queue import enqueue_export, claim_next_export, reap_stale_exports

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _source(db) -> uuid.UUID:
    v = Vendor(name="V")
    db.add(v)
    await db.flush()
    s = DocumentationSource(vendor_id=v.id, name="S", base_url="http://x")
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s.id


@pytest.mark.asyncio
async def test_enqueue_export_creates_pending(sessions):
    async with sessions() as db:
        sid = await _source(db)
        job = await enqueue_export(db, sid, {"source_id": str(sid), "format": "pdf"})
        assert job.status == ExportStatus.PENDING
        assert job.request["format"] == "pdf"


@pytest.mark.asyncio
async def test_claim_marks_running(sessions):
    async with sessions() as db:
        sid = await _source(db)
        await enqueue_export(db, sid, {"source_id": str(sid)})
    async with sessions() as db:
        job = await claim_next_export(db, "worker-1")
        assert job is not None and job.status == ExportStatus.RUNNING
        assert job.claimed_by == "worker-1" and job.attempts == 1 and job.heartbeat_at is not None
    async with sessions() as db:
        assert await claim_next_export(db, "worker-2") is None


@pytest.mark.asyncio
async def test_reap_requeues_then_fails_at_cap(sessions):
    async with sessions() as db:
        sid = await _source(db)
        job = ExportJob(
            source_id=sid, request={}, status=ExportStatus.RUNNING, attempts=1,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add(job)
        await db.commit()
        jid = job.id
    async with sessions() as db:
        assert await reap_stale_exports(db) == 1
    async with sessions() as db:
        job = (await db.execute(select(ExportJob).where(ExportJob.id == jid))).scalar_one()
        assert job.status == ExportStatus.PENDING and job.claimed_by is None
    async with sessions() as db:
        job = (await db.execute(select(ExportJob).where(ExportJob.id == jid))).scalar_one()
        job.status = ExportStatus.RUNNING
        job.attempts = 3
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
    async with sessions() as db:
        await reap_stale_exports(db)
    async with sessions() as db:
        job = (await db.execute(select(ExportJob).where(ExportJob.id == jid))).scalar_one()
        assert job.status == ExportStatus.FAILED
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_export_queue.py -v`
Expected: FAIL — `ImportError` for the queue functions.

- [ ] **Step 7: Implement the queue functions**

In `backend/app/services/queue.py`, add the import `from app.models.export_job import ExportJob, ExportStatus` (next to the ExtractionRun import) and append:
```python
async def enqueue_export(
    db: AsyncSession, source_id: uuid.UUID, request: dict
) -> ExportJob:
    """Insert a pending export job."""
    job = ExportJob(source_id=source_id, request=request, status=ExportStatus.PENDING)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def claim_next_export(
    db: AsyncSession, worker_id: str
) -> ExportJob | None:
    """Atomically claim the oldest pending export job, or None if empty."""
    result = await db.execute(
        select(ExportJob)
        .where(ExportJob.status == ExportStatus.PENDING)
        .order_by(ExportJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    now = datetime.now(timezone.utc)
    job.status = ExportStatus.RUNNING
    job.claimed_by = worker_id
    job.claimed_at = now
    job.heartbeat_at = now
    job.started_at = now
    job.attempts += 1
    await db.commit()
    await db.refresh(job)
    return job


async def reap_stale_exports(
    db: AsyncSession, max_attempts: int = 3, stale_seconds: int = 300
) -> int:
    """Requeue (or fail, at the attempt cap) export jobs whose worker stopped heartbeating."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    result = await db.execute(
        select(ExportJob)
        .where(
            ExportJob.status == ExportStatus.RUNNING,
            or_(ExportJob.heartbeat_at.is_(None), ExportJob.heartbeat_at < cutoff),
        )
        .with_for_update(skip_locked=True)
    )
    stale = result.scalars().all()
    for job in stale:
        if job.attempts >= max_attempts:
            job.status = ExportStatus.FAILED
            job.error_message = (job.error_message or "worker lost")[:4096]
            job.completed_at = datetime.now(timezone.utc)
        else:
            job.status = ExportStatus.PENDING
            job.claimed_by = None
            job.claimed_at = None
            job.heartbeat_at = None
    await db.commit()
    return len(stale)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_export_queue.py -v`
Expected: 3 PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/export_job.py backend/app/models/__init__.py backend/alembic/versions/f5a6b7c8d9e0_add_export_jobs.py backend/app/services/queue.py backend/tests/test_export_queue.py
git commit -m "feat(export): export_jobs queue table + enqueue/claim/reap"
```

---

### Task 3: Bounded-memory two-pass generation in `ExportEngine`

**Files:**
- Modify: `backend/app/services/exporter.py`
- Test: `backend/tests/test_integration.py`

**Interfaces:**
- Consumes: `render_markdown_to_pdf` (existing); `pypdf` (Task 1).
- Produces: `export`/`export_sync` keep their signatures and return dict shape; internally they do a metadata-only plan pass and a per-chunk render pass. PDF output is a `pypdf`-merged file.

- [ ] **Step 1: Write the failing/regression tests**

Add to `backend/tests/test_integration.py`:
```python
from pypdf import PdfReader  # add near the other imports


def test_export_pdf_merges_per_chapter(db_session):
    v = Vendor(name="MergeVendor")
    db_session.add(v); db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="MergeSrc", base_url="https://m.com")
    db_session.add(s); db_session.flush()
    # Two top-level chapters, 2 articles each.
    ch1 = TOCEntry(source_id=s.id, title="Chapter 1", url=None, level=0, sort_order=0, is_article=False)
    ch2 = TOCEntry(source_id=s.id, title="Chapter 2", url=None, level=0, sort_order=3, is_article=False)
    db_session.add_all([ch1, ch2]); db_session.flush()
    arts = []
    for ci, ch in enumerate((ch1, ch2)):
        for j in range(2):
            t = TOCEntry(source_id=s.id, title=f"c{ci}a{j}", url=f"https://m.com/{ci}/{j}",
                         level=1, sort_order=ci * 10 + j + 1, is_article=True, parent_id=ch.id)
            db_session.add(t); db_session.flush()
            arts.append(Article(
                source_id=s.id, toc_entry_id=t.id, title=f"c{ci}a{j}",
                source_url=f"https://m.com/{ci}/{j}", content_markdown=f"# c{ci}a{j}\n\nbody",
                sort_order=ci * 10 + j + 1, estimated_tokens=50, content_size_bytes=200,
            ))
    db_session.add_all(arts); db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(db_session, source_id=s.id, format="pdf")
    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    pdfs = [f for f in os.listdir(export_dir) if f.endswith(".pdf")]
    assert len(pdfs) == 1
    reader = PdfReader(os.path.join(export_dir, pdfs[0]))
    # Merged from a header page + 2 chapter chunks -> at least 3 pages, and valid.
    assert len(reader.pages) >= 3
    # No leftover temp chapter PDFs.
    assert not any(f.startswith("_chunk") for f in os.listdir(export_dir))
```
(The existing `test_export_full`, `test_export_pdf_full`, `test_export_pdf_split_produces_multiple_pdfs`, `test_export_partial_by_articles`, `test_export_by_topic_search` MUST still pass — they assert the unchanged output content/shape.)

- [ ] **Step 2: Run to verify the new test fails**

Run: `cd backend && pytest tests/test_integration.py -k merges_per_chapter -v`
Expected: FAIL (today's PDF path renders one document; this asserts the merged multi-chunk structure / no temp files — adjust once implemented).

- [ ] **Step 3: Add helpers + chunk constant**

In `backend/app/services/exporter.py`: add imports `from sqlalchemy.orm import load_only` (extend the existing `sqlalchemy.orm` import) and `from pypdf import PdfWriter`, and a module constant `_RENDER_CHUNK = 50`.

Split `_build_markdown_document` into a header builder and a per-article section builder (keep `_build_markdown_document` as a thin wrapper so existing call sites/tests are unaffected):
```python
    def _doc_header(self, source_name: str, titles: Sequence[str], count: int) -> str:
        lines = [f"# {source_name}", "",
                 f"> Extracted: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                 f"> Articles: {count}", "", "---", "", "## Table of Contents", ""]
        for i, title in enumerate(titles, 1):
            lines.append(f"{i}. [{title}](#{self._slugify(title)})")
        lines += ["", "---", ""]
        return "\n".join(lines)

    def _article_section(self, article: Article) -> str:
        lines = [f"## {article.title}", "",
                 f"**Source:** [{article.source_url}]({article.source_url})"]
        if article.last_updated_at:
            lines.append(f"**Last Updated:** {article.last_updated_at.strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append(f"**Extracted:** {article.extracted_at.strftime('%Y-%m-%d %H:%M UTC')}")
        lines += ["", article.content_markdown, "", "---", ""]
        return "\n".join(lines)

    def _build_markdown_document(self, articles: Sequence[Article], source_name: str) -> str:
        titles = [a.title for a in articles]
        body = "".join(self._article_section(a) + "\n" for a in articles)
        return self._doc_header(source_name, titles, len(articles)) + "\n" + body

    def _render_chunks(self, group: list[Article]) -> list[list[Article]]:
        """Split one output group into render chunks of <= _RENDER_CHUNK articles,
        preserving order (memory bound per render)."""
        return [group[i:i + _RENDER_CHUNK] for i in range(0, len(group), _RENDER_CHUNK)]
```

- [ ] **Step 4: Make `_resolve_articles` metadata-only when planning**

Add a `meta_only: bool = False` parameter to BOTH `_resolve_articles` (async) and `_resolve_articles_sync`. When `meta_only` is True, replace `.options(selectinload(Article.images), selectinload(Article.toc_entry))` with:
```python
        .options(load_only(
            Article.id, Article.title, Article.sort_order,
            Article.toc_entry_id, Article.content_size_bytes, Article.estimated_tokens,
        ))
```
(When False, keep the existing full load — `export_sync`/`export` will pass `meta_only=True` for planning, then load content per chunk via the loaders below.)

- [ ] **Step 5: Add per-chunk content loaders**

Add to `ExportEngine` an async and a sync loader that fetch full articles for a chunk and preserve the requested order:
```python
    async def _load_chunk(self, db: AsyncSession, ids: list[uuid.UUID]) -> list[Article]:
        rows = (await db.execute(
            select(Article).where(Article.id.in_(ids)).options(selectinload(Article.images))
        )).scalars().all()
        by_id = {a.id: a for a in rows}
        return [by_id[i] for i in ids if i in by_id]

    def _load_chunk_sync(self, db: Session, ids: list[uuid.UUID]) -> list[Article]:
        rows = db.execute(
            select(Article).where(Article.id.in_(ids)).options(selectinload(Article.images))
        ).scalars().all()
        by_id = {a.id: a for a in rows}
        return [by_id[i] for i in ids if i in by_id]
```

- [ ] **Step 6: Rewrite `_generate_export` to take a content loader and render per chunk**

Change `_generate_export`'s signature to accept a `load_content` callable instead of full articles up front:
```python
    def _generate_export(
        self,
        groups: list[list[Article]],          # plan-pass (metadata-only) groups
        source_name: str,
        source_id: uuid.UUID,
        format: str,
        load_content,                          # Callable[[list[uuid.UUID]], list[Article]]
    ) -> dict:
```
Body:
```python
        export_id = uuid.uuid4()
        export_subdir = os.path.join(self.export_dir, str(export_id))
        os.makedirs(export_subdir, exist_ok=True)

        archive_members: list[tuple[str, str]] = []
        files_info: list[dict] = []
        total_size = 0
        base_name = source_name.replace(" ", "_")
        ext = "pdf" if format == "pdf" else "md"

        for gi, group in enumerate(groups, 1):
            filename = f"{base_name}.{ext}" if len(groups) == 1 else f"{base_name}_part{gi:03d}.{ext}"
            filepath = os.path.join(export_subdir, filename)
            titles = [a.title for a in group]
            group_tokens = sum(a.estimated_tokens for a in group)

            if format == "pdf":
                chunk_pdfs: list[str] = []
                # Header/TOC page first.
                header_md = self._doc_header(source_name, titles, len(group))
                header_pdf = os.path.join(export_subdir, f"_chunk_{gi}_000.pdf")
                with open(header_pdf, "wb") as f:
                    f.write(render_markdown_to_pdf(header_md, base_url=self.media_root + os.sep))
                chunk_pdfs.append(header_pdf)
                for ci, chunk in enumerate(self._render_chunks(group), 1):
                    full = load_content([a.id for a in chunk])
                    chunk_md = "".join(self._article_section(a) + "\n" for a in full)
                    chunk_md = chunk_md.replace(f"{settings.media_url_prefix}/", "")
                    cpath = os.path.join(export_subdir, f"_chunk_{gi}_{ci:03d}.pdf")
                    with open(cpath, "wb") as f:
                        f.write(render_markdown_to_pdf(chunk_md, base_url=self.media_root + os.sep))
                    chunk_pdfs.append(cpath)
                writer = PdfWriter()
                for cp in chunk_pdfs:
                    writer.append(cp)
                with open(filepath, "wb") as f:
                    writer.write(f)
                writer.close()
                for cp in chunk_pdfs:
                    os.remove(cp)
                file_size = os.path.getsize(filepath)
            else:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(self._doc_header(source_name, titles, len(group)) + "\n")
                    for chunk in self._render_chunks(group):
                        full = load_content([a.id for a in chunk])
                        for a in full:
                            section = self._article_section(a).replace(
                                f"{settings.media_url_prefix}/", "images/"
                            )
                            f.write(section + "\n")
                            for image in a.images:
                                self._copy_image(a.id, image, export_subdir, archive_members)
                file_size = os.path.getsize(filepath)

            archive_members.append((filepath, filename))
            total_size += file_size
            files_info.append({
                "filename": filename, "article_count": len(group), "size_bytes": file_size,
                "estimated_tokens": group_tokens,
                "first_article_title": group[0].title, "last_article_title": group[-1].title,
            })

        # Bundle everything into a single self-contained zip.
        zip_filename = f"{base_name}.zip"
        zip_path = os.path.join(export_subdir, zip_filename)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arcname in archive_members:
                zf.write(abs_path, arcname)

        return {
            "export_id": export_id, "source_id": source_id, "file_count": len(files_info),
            "total_articles": sum(f["article_count"] for f in files_info),
            "total_size_bytes": total_size, "zip_filename": zip_filename, "files": files_info,
        }
```
Add the dedup-aware image copy helper (extracted from the old loop):
```python
    def _copy_image(self, article_id, image, export_subdir, archive_members) -> None:
        rel = os.path.join(str(article_id), image.local_filename)
        dst_path = os.path.join(export_subdir, "images", rel)
        if os.path.exists(dst_path):
            return
        src_path = os.path.join(self.media_root, rel)
        if not os.path.isfile(src_path):
            return
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)
        archive_members.append((dst_path, os.path.join("images", rel)))
```

- [ ] **Step 7: Make `export_sync` the single plan-then-generate entry; remove the async `export` path**

Generation is synchronous (it writes files and renders PDFs). The route no longer generates — it enqueues — and the worker runs generation on a **sync** session (Task 4). So `export_sync` becomes the authoritative generation entry and the async `export` method is removed.

Update the tail of `export_sync` to plan (metadata-only) then generate with a sync chunk loader:
```python
        articles = self._resolve_articles_sync(
            db, source_id, article_ids, toc_entry_ids, topic_query, meta_only=True
        )
        if not articles:
            raise ValueError("No articles matched the selection criteria")
        chapter_keys = None
        if respect_chapters and split_by:
            toc = db.execute(
                select(TOCEntry.id, TOCEntry.parent_id).where(TOCEntry.source_id == source_id)
            )
            chapter_keys = self._chapter_keys(toc.all(), articles)
        if split_by:
            groups = self._split_articles(
                articles, split_by, max_articles_per_file, max_file_size_bytes,
                max_tokens_per_file, respect_chapters, chapter_keys,
            )
        else:
            groups = [articles]
        import functools
        return self._generate_export(
            groups, source.name, source_id, format, functools.partial(self._load_chunk_sync, db)
        )
```

Then **delete** the async `export` method and the async `_load_chunk` / async-only `_resolve_articles` if nothing else references them (grep `export_engine.export(` and `_resolve_articles(` — the route call is removed in Task 5, so the async `export` becomes dead). Removing dead code keeps the engine focused; if a reference remains, leave the method but route it through the same plan-then-`_generate_export` flow using `_load_chunk`/`_resolve_articles(meta_only=True)`.

- [ ] **Step 8: Run the export suite**

Run: `cd backend && pytest tests/test_integration.py -v`
Expected: the new `merges_per_chapter` test PASSES and all existing export tests (markdown full/partial/topic, pdf full/split) PASS unchanged.

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/exporter.py backend/tests/test_integration.py
git commit -m "refactor(export): bounded two-pass generation; per-chunk PDF render + pypdf merge"
```

---

### Task 4: Worker + scheduler integration

**Files:**
- Create: `backend/app/services/export_runner.py`
- Modify: `backend/app/worker.py`
- Modify: `backend/app/services/scheduling.py`
- Test: `backend/tests/test_export_worker.py`

**Interfaces:**
- Consumes: `claim_next_export`/`reap_stale_exports` (Task 2); `ExportEngine.export_sync` (Task 3).
- Produces: `run_export_job(export_job_id) -> None` (executes a claimed export job to completion/failure using a sync session); worker `run_one` also claims export jobs; scheduler tick reaps stale exports.

- [ ] **Step 1: Write the failing worker test**

Create `backend/tests/test_export_worker.py` (sync `db_session` fixture like `test_integration.py`, plus a direct call to the runner). Seed a source with a few articles, `enqueue_export` (via a sync insert of an `ExportJob` row), then call `run_export_job(job_id)` and assert the job is `COMPLETED` with `export_id` set and a `.zip` on disk:
```python
import os, sys, uuid
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, DocumentationSource, Article, ExportJob
from app.models.export_job import ExportStatus
from app.services.export_runner import run_export_job_sync
from app.services.exporter import export_engine

URL = settings.database_url_sync.rsplit("/", 1)[0] + "/docextractor_test"
eng = create_engine(URL); SyncS = sessionmaker(eng, class_=Session, expire_on_commit=False)


@pytest.fixture
def db():
    Base.metadata.drop_all(eng); Base.metadata.create_all(eng)
    s = SyncS(); yield s; s.close(); Base.metadata.drop_all(eng)


def test_run_export_job_completes(db):
    v = Vendor(name="EJ"); db.add(v); db.flush()
    s = DocumentationSource(vendor_id=v.id, name="EJSrc", base_url="https://ej.com")
    db.add(s); db.flush()
    for i in range(3):
        db.add(Article(source_id=s.id, title=f"A{i}", source_url=f"https://ej.com/{i}",
                       content_markdown=f"# A{i}\n\nx", sort_order=i,
                       estimated_tokens=10, content_size_bytes=50))
    job = ExportJob(source_id=s.id, request={"source_id": str(s.id), "format": "pdf"},
                    status=ExportStatus.RUNNING)
    db.add(job); db.commit()
    jid = job.id

    run_export_job_sync(jid, session_factory=SyncS)

    db2 = SyncS()
    job = db2.execute(select(ExportJob).where(ExportJob.id == jid)).scalar_one()
    assert job.status == ExportStatus.COMPLETED
    assert job.export_id is not None
    export_dir = os.path.join(export_engine.export_dir, str(job.export_id))
    assert any(f.endswith(".zip") for f in os.listdir(export_dir))
    db2.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && pytest tests/test_export_worker.py -v`
Expected: FAIL — `app.services.export_runner` missing.

- [ ] **Step 3: Implement the export runner**

Create `backend/app/services/export_runner.py`:
```python
"""Execute a claimed export job (synchronous generation) and record the result."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import sync_session  # see note below
from app.models.export_job import ExportJob, ExportStatus
from app.services.exporter import export_engine

logger = logging.getLogger(__name__)


def run_export_job_sync(job_id: uuid.UUID, session_factory=None) -> None:
    """Generate the export for a RUNNING job and mark it completed/failed."""
    factory = session_factory or sync_session
    with factory() as db:
        job = db.execute(select(ExportJob).where(ExportJob.id == job_id)).scalar_one_or_none()
        if job is None:
            return
        req = dict(job.request)
        source_id = uuid.UUID(req["source_id"])
        try:
            result = export_engine.export_sync(
                db,
                source_id=source_id,
                article_ids=[uuid.UUID(x) for x in req["article_ids"]] if req.get("article_ids") else None,
                toc_entry_ids=[uuid.UUID(x) for x in req["toc_entry_ids"]] if req.get("toc_entry_ids") else None,
                topic_query=req.get("topic_query"),
                split_by=req.get("split_by"),
                max_articles_per_file=req.get("max_articles_per_file"),
                max_file_size_bytes=req.get("max_file_size_bytes"),
                max_tokens_per_file=req.get("max_tokens_per_file"),
                respect_chapters=req.get("respect_chapters", False),
                format=req.get("format", "markdown"),
            )
            job.export_id = result["export_id"]
            job.result = {k: v for k, v in result.items() if k != "export_id"}
            job.result["export_id"] = str(result["export_id"])
            job.result["source_id"] = str(result["source_id"])
            job.status = ExportStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            logger.exception("Export job %s failed", job_id)
            db.rollback()
            job = db.execute(select(ExportJob).where(ExportJob.id == job_id)).scalar_one()
            job.status = ExportStatus.FAILED
            job.error_message = str(exc)[:4096]
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
```
Add a synchronous session factory in `backend/app/core/database.py` if one does not already exist:
```python
from sqlalchemy import create_engine as _create_sync_engine
from sqlalchemy.orm import sessionmaker as _sync_sessionmaker, Session as _SyncSession

sync_engine = _create_sync_engine(settings.database_url_sync, pool_pre_ping=True)
sync_session = _sync_sessionmaker(sync_engine, class_=_SyncSession, expire_on_commit=False)
```

- [ ] **Step 4: Run the worker test to verify it passes**

Run: `cd backend && pytest tests/test_export_worker.py -v`
Expected: PASS.

- [ ] **Step 5: Wire export claiming into the worker loop**

In `backend/app/worker.py`: import `claim_next_export` and `run_export_job_sync`, and extend `run_one` so that after a `None` extraction claim it tries an export claim. Add (after the extraction-run block returns False today):
```python
    # No extraction run available — try an export job.
    async with claim_session_factory() as db:
        job = await claim_next_export(db, WORKER_ID)
        if job is None:
            return False
        job_id = job.id

    hb = asyncio.create_task(_heartbeat_export(job_id, work_session_factory))
    try:
        # Generation is synchronous; run it off the event loop.
        await asyncio.to_thread(run_export_job_sync, job_id)
    finally:
        hb.cancel()
    return True
```
Add an export heartbeat that bumps `ExportJob.heartbeat_at` (mirror `_heartbeat`, but update `ExportJob`). Refactor the existing `_heartbeat` to take a model, or add `_heartbeat_export`:
```python
async def _heartbeat_export(job_id, session_factory):
    from app.models.export_job import ExportJob
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            async with session_factory() as db:
                await db.execute(update(ExportJob).where(ExportJob.id == job_id)
                                 .values(heartbeat_at=datetime.now(timezone.utc)))
                await db.commit()
        except Exception:
            logger.exception("Export heartbeat failed for %s", job_id)
```
Restructure `run_one` so the existing extraction path returns early when it handled a run, and the export path above runs when no run was claimed. (Keep the existing extraction logic intact; only add the export branch when `claim_next_run` returned None.)

- [ ] **Step 6: Add export reaping to the scheduler tick**

In `backend/app/services/scheduling.py`: import `reap_stale_exports` and call it in `tick` right after `reaped = await reap_stale_runs(db)`:
```python
    reaped_exports = await reap_stale_exports(db)
```
and include it in the returned dict: `"reaped_exports": reaped_exports`.

- [ ] **Step 7: Run the full backend suite**

Run: `cd backend && pytest -q`
Expected: all pass (existing + new export queue/worker/generation tests).

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/export_runner.py backend/app/worker.py backend/app/services/scheduling.py backend/app/core/database.py backend/tests/test_export_worker.py
git commit -m "feat(export): worker runs export jobs; scheduler reaps stale ones"
```

---

### Task 5: API — enqueue + job status

**Files:**
- Modify: `backend/app/schemas/export.py`
- Modify: `backend/app/routes/export.py`
- Test: `backend/tests/test_versions.py` (or a new async route test using the same `client` fixture)

**Interfaces:**
- Consumes: `enqueue_export` (Task 2).
- Produces: `POST /api/export` → `{export_job_id, status}`; `GET /api/export/jobs/{id}` → job status + result.

- [ ] **Step 1: Add response schemas**

In `backend/app/schemas/export.py` add:
```python
class ExportJobCreatedResponse(BaseModel):
    export_job_id: uuid.UUID
    status: str


class ExportJobStatusResponse(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    status: str
    export_id: uuid.UUID | None
    zip_filename: str | None
    files: list[ExportFileInfo] | None
    error_message: str | None
```

- [ ] **Step 2: Write the failing route test**

Add to `backend/tests/test_versions.py` (uses the async `client` fixture):
```python
@pytest.mark.asyncio
async def test_export_enqueue_and_status(client):
    c, sf = client
    async with sf() as db:
        v = Vendor(name="ExpRoute"); db.add(v); await db.flush()
        s = DocumentationSource(vendor_id=v.id, name="ExpRouteSrc", base_url="https://er.com")
        db.add(s); await db.flush()
        db.add(Article(source_id=s.id, title="A", source_url="https://er.com/a",
                       content_markdown="# A\n\nx", sort_order=0,
                       estimated_tokens=1, content_size_bytes=1))
        await db.commit()
        sid = str(s.id)

    r = await c.post("/api/export", json={"source_id": sid, "format": "markdown"})
    assert r.status_code == 200
    jid = r.json()["export_job_id"]
    assert r.json()["status"] == "pending"

    g = await c.get(f"/api/export/jobs/{jid}")
    assert g.status_code == 200
    assert g.json()["status"] == "pending"  # worker not running in this test
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && pytest tests/test_versions.py -k export_enqueue -v`
Expected: FAIL — `POST /api/export` not found (404).

- [ ] **Step 4: Implement the routes**

In `backend/app/routes/export.py`: import `enqueue_export`, `ExportJob`, `ExportStatus`, and the new schemas. Add:
```python
@router.post("", response_model=ExportJobCreatedResponse)
async def create_export(body: ExportRequest, db: AsyncSession = Depends(get_db)):
    """Enqueue an export job; the worker generates it. Poll /api/export/jobs/{id}."""
    src = await db.execute(
        select(DocumentationSource.id).where(DocumentationSource.id == body.source_id)
    )
    if src.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Source not found")
    job = await enqueue_export(db, body.source_id, body.model_dump(mode="json"))
    return ExportJobCreatedResponse(export_job_id=job.id, status="pending")


@router.get("/jobs/{job_id}", response_model=ExportJobStatusResponse)
async def get_export_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = (await db.execute(select(ExportJob).where(ExportJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    result = job.result or {}
    return ExportJobStatusResponse(
        id=job.id, source_id=job.source_id, status=job.status.value,
        export_id=job.export_id, zip_filename=result.get("zip_filename"),
        files=result.get("files"), error_message=job.error_message,
    )
```
Add the needed imports at the top: `from sqlalchemy import select`, `from sqlalchemy.ext.asyncio import AsyncSession` (already present), `from app.models.source import DocumentationSource`, `from app.models.export_job import ExportJob, ExportStatus`, `from app.services.queue import enqueue_export`, and the new schema names. Keep the old `POST /api/export/markdown` removed (replaced by `POST /api/export`); keep the two `download` routes and `list`.

- [ ] **Step 5: Run tests**

Run: `cd backend && pytest tests/test_versions.py -k export_enqueue -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/export.py backend/app/routes/export.py backend/tests/test_versions.py
git commit -m "feat(api): enqueue export jobs and poll job status"
```

---

### Task 6: Frontend — enqueue, poll, download

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/ExportPanel.tsx`

**Interfaces:**
- Consumes: `POST /api/export`, `GET /api/export/jobs/{id}` (Task 5).

- [ ] **Step 1: Types + client**

In `frontend/src/types/index.ts` add:
```typescript
export interface ExportJobCreated {
  export_job_id: string;
  status: string;
}
export interface ExportJobStatus {
  id: string;
  source_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  export_id: string | null;
  zip_filename: string | null;
  files: ExportFileInfo[] | null;
  error_message: string | null;
}
```
In `frontend/src/api/client.ts` replace `exportMarkdown` with enqueue + status (drop the 600s timeout — enqueue is fast):
```typescript
export async function enqueueExport(data: ExportRequest): Promise<ExportJobCreated> {
  const res = await api.post("/export", data);
  return res.data;
}
export async function getExportJob(jobId: string): Promise<ExportJobStatus> {
  const res = await api.get(`/export/jobs/${jobId}`);
  return res.data;
}
```

- [ ] **Step 2: Poll in ExportPanel**

In `frontend/src/components/ExportPanel.tsx`, change `handleExport` to enqueue then poll. Replace the `exportMarkdown({...})` call with `enqueueExport({...})`, store `export_job_id`, and poll `getExportJob` every 2s via `setInterval` (clear on completed/failed/unmount). On `completed`, set `exportResult` from `{export_id, files, zip_filename}` (reuse the existing `ExportResponse` display, mapping the job status' fields); on `failed`, show `error_message`. Show a "Queued… / Generating…" message while `pending`/`running`. Use a `useEffect` cleanup to clear the interval. (Keep the existing result/download UI; only the acquisition changes.)

- [ ] **Step 3: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds; no new lint errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/ExportPanel.tsx
git commit -m "feat(ui): enqueue export jobs and poll for completion"
```

---

### Task 7: Live container verification

**Files:** none.

- [ ] **Step 1: Rebuild + start**

Run: `docker compose up -d --build backend worker scheduler frontend`
Expected: all start; `docker compose logs worker | tail` shows the worker started.

- [ ] **Step 2: Enqueue a PDF export of Clumio and poll**

```bash
SID=$(curl -s http://localhost:8000/api/sources | python3 -c "import sys,json;print(json.load(sys.stdin)['sources'][0]['id'])")
JID=$(curl -s -X POST http://localhost:8000/api/export -H 'Content-Type: application/json' -d "{\"source_id\":\"$SID\",\"format\":\"pdf\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['export_job_id'])")
echo "job=$JID"
for i in $(seq 1 60); do
  S=$(curl -s http://localhost:8000/api/export/jobs/$JID | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'], d.get('zip_filename'))")
  echo "[$i] $S"; [ "${S%% *}" = "completed" ] && break; [ "${S%% *}" = "failed" ] && break; sleep 3
done
```
Expected: transitions `pending`→`running`→`completed` with a `zip_filename`; the request returns immediately (no long blocking POST).

- [ ] **Step 3: Download + validate**

```bash
EID=$(curl -s http://localhost:8000/api/export/jobs/$JID | python3 -c "import sys,json;print(json.load(sys.stdin)['export_id'])")
curl -s "http://localhost:8000/api/export/download/$EID" -o /tmp/async_clumio.zip
python3 -c "import zipfile,io;z=zipfile.ZipFile('/tmp/async_clumio.zip');n=[x for x in z.namelist() if x.endswith('.pdf')][0];print('members:',z.namelist());print('pdf magic:',z.read(n)[:5])"
```
Expected: a valid `%PDF-` in the bundle.

- [ ] **Step 4: Verify the UI path**

In the browser (http://localhost:3000) export Clumio as PDF: the button shows Queued…/Generating…, then a download link appears when the job completes. No "Export failed".

---

## Self-Review

**Spec coverage:**
- `export_jobs` table + queue fns + worker-claims-both + reaper → Tasks 2, 4.
- Plan pass metadata-only; per-chunk render pass; PDF per-chapter render + pypdf merge; markdown incremental → Task 3.
- `pypdf` dep → Task 1.
- `POST /api/export` enqueue + `GET /api/export/jobs/{id}` + download unchanged → Task 5.
- Frontend enqueue/poll/download, remove long timeout → Task 6.
- Testing: queue (Task 2), worker (Task 4), bounded generation + merged PDF + regression (Task 3), route (Task 5), live (Task 7), frontend build/lint (Task 6).

**Placeholder scan:** No TBD. Task 2 Step 3 has a deliberate `<CURRENT_HEAD>` to be filled from `alembic heads` (can't be hardcoded across stacked branches) — the step says exactly how to obtain it. Task 3 Step 7 makes `export_sync` the single synchronous generation entry (the worker drives it) and removes the async `export` path. Task 6 Step 2 is prose because the polling rewire depends on the existing component structure; it names the exact functions and behaviour.

**Type consistency:** `ExportStatus` values stored UPPERCASE; `enqueue_export(db, source_id, request: dict)`, `claim_next_export(db, worker_id)`, `reap_stale_exports(db, ...)`, `run_export_job_sync(job_id, session_factory=None)`, and `export_sync(..., format=...)` are used identically across tasks. `_generate_export(groups, source_name, source_id, format, load_content)` matches its callers; `_load_chunk_sync(db, ids) -> list[Article]` matches the partial passed in. The route returns `export_job_id`/`status` consumed by the TS `ExportJobCreated`.

## Out of scope (from the spec)
Read-path scaling; live large-source stress test; dedicated export worker / fairness tuning; global cross-page PDF TOC; export retention policy.
