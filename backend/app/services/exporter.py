"""Markdown export engine — full, partial, and split exports.

Two-pass design:
  1. Plan pass  — loads only lightweight metadata columns (id, title, sort_order,
                  toc_entry_id, content_size_bytes, estimated_tokens) to determine
                  grouping/splitting without pulling article content into memory.
  2. Render pass — loads one render-chunk's full content at a time and writes
                   output incrementally; peak memory ≈ one chunk.

For PDF output each render chunk is rendered to a temporary PDF and merged with
pypdf.PdfWriter; temp files are removed afterwards so only the final merged PDF
remains in the export directory.
"""

import functools
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session, load_only, selectinload
from pypdf import PdfWriter

from app.core.config import settings
from app.models.article import Article
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry
from app.services.pdf_renderer import render_markdown_to_pdf

# Full-text search expression over title + content. Kept identical to the GIN
# expression index (see the add_fts_index migration) so the planner can use it.
_TSV = (
    "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content_markdown,''))"
)

# Max articles loaded per render pass (memory bound).
_RENDER_CHUNK = 50


class ExportEngine:
    """Builds markdown export files from extracted articles."""

    def __init__(self):
        self.export_dir = os.path.abspath(settings.export_dir)
        self.media_root = os.path.abspath(settings.media_dir)
        os.makedirs(self.export_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Plan-pass article resolution                                        #
    # ------------------------------------------------------------------ #

    def _resolve_articles_sync(
        self,
        db: Session,
        source_id: uuid.UUID,
        article_ids: list[uuid.UUID] | None = None,
        toc_entry_ids: list[uuid.UUID] | None = None,
        topic_query: str | None = None,
        meta_only: bool = False,
    ) -> list[Article]:
        """Synchronous article resolution.

        When *meta_only* is True only lightweight planning columns are loaded
        (id, title, sort_order, toc_entry_id, content_size_bytes,
        estimated_tokens).  When False the full article graph is loaded
        (images, toc_entry) as before.
        """
        if meta_only:
            load_opts = load_only(
                Article.id, Article.title, Article.sort_order,
                Article.toc_entry_id, Article.content_size_bytes,
                Article.estimated_tokens,
            )
        else:
            load_opts = selectinload(Article.images), selectinload(Article.toc_entry)

        def _base_query():
            q = select(Article).where(Article.source_id == source_id)
            if meta_only:
                q = q.options(load_opts)
            else:
                q = q.options(*load_opts)
            return q

        query = _base_query()

        if article_ids:
            query = query.where(Article.id.in_(article_ids))
        elif toc_entry_ids:
            toc_ids_set = set(toc_entry_ids)
            all_toc = db.execute(
                select(TOCEntry).where(TOCEntry.source_id == source_id)
            )
            toc_entries = all_toc.scalars().all()

            children_map: dict[uuid.UUID, list[uuid.UUID]] = {}
            for te in toc_entries:
                if te.parent_id:
                    children_map.setdefault(te.parent_id, []).append(te.id)

            expanded = set(toc_ids_set)
            queue = list(toc_ids_set)
            while queue:
                tid = queue.pop()
                for child_id in children_map.get(tid, []):
                    if child_id not in expanded:
                        expanded.add(child_id)
                        queue.append(child_id)

            query = query.where(Article.toc_entry_id.in_(expanded))
        elif topic_query:
            query = query.where(
                text(f"{_TSV} @@ plainto_tsquery('english', :q)").bindparams(
                    q=topic_query
                )
            ).order_by(
                text(f"ts_rank({_TSV}, plainto_tsquery('english', :qr)) DESC").bindparams(
                    qr=topic_query
                ),
                Article.sort_order,
            )
            result = db.execute(query)
            return list(result.scalars().all())

        query = query.order_by(Article.sort_order)
        result = db.execute(query)
        return list(result.scalars().all())

    # ------------------------------------------------------------------ #
    #  Per-chunk content loaders (render pass)                             #
    # ------------------------------------------------------------------ #

    def _load_chunk_sync(self, db: Session, ids: list[uuid.UUID]) -> list[Article]:
        rows = db.execute(
            select(Article).where(Article.id.in_(ids)).options(selectinload(Article.images))
        ).scalars().all()
        by_id = {a.id: a for a in rows}
        return [by_id[i] for i in ids if i in by_id]

    # ------------------------------------------------------------------ #
    #  Document builders                                                   #
    # ------------------------------------------------------------------ #

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
        """Build a single markdown document from a list of articles (legacy thin wrapper)."""
        titles = [a.title for a in articles]
        body = "".join(self._article_section(a) + "\n" for a in articles)
        return self._doc_header(source_name, titles, len(articles)) + "\n" + body

    def _render_chunks(self, group: list[Article]) -> list[list[Article]]:
        """Split one output group into render chunks of <= _RENDER_CHUNK articles,
        preserving order (memory bound per render)."""
        return [group[i:i + _RENDER_CHUNK] for i in range(0, len(group), _RENDER_CHUNK)]

    def _slugify(self, text: str) -> str:
        """Create a GitHub-flavored markdown anchor slug."""
        slug = text.lower().strip()
        slug = "".join(c if c.isalnum() or c in " -_" else "" for c in slug)
        slug = slug.replace(" ", "-")
        # Collapse multiple dashes
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug.strip("-")

    # ------------------------------------------------------------------ #
    #  Splitting helpers                                                   #
    # ------------------------------------------------------------------ #

    def _article_metric(self, article: Article, split_by: str) -> int:
        """The quantity a split limit is measured in, per article."""
        if split_by == "size":
            return article.content_size_bytes
        if split_by == "tokens":
            return article.estimated_tokens
        return 1  # "articles"

    def _split_limit(
        self,
        split_by: str,
        max_articles: int | None,
        max_size: int | None,
        max_tokens: int | None,
    ) -> int:
        if split_by == "size":
            return max_size or settings.max_file_size_bytes
        if split_by == "tokens":
            return max_tokens or settings.max_tokens_per_file
        return max_articles or settings.max_articles_per_file

    def _chapter_keys(
        self, toc_rows: Sequence, articles: Sequence[Article]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Map each article to its top-level TOC ancestor (its "chapter").

        toc_rows is a sequence of (id, parent_id) tuples. Articles with no TOC
        entry (e.g. orphaned/removed pages) map to None — treated as one chapter.
        """
        parent = {row[0]: row[1] for row in toc_rows}

        def root(tid: uuid.UUID | None) -> uuid.UUID | None:
            seen: set[uuid.UUID] = set()
            while tid is not None and parent.get(tid) is not None and tid not in seen:
                seen.add(tid)
                tid = parent[tid]
            return tid

        return {a.id: root(a.toc_entry_id) for a in articles}

    def _split_by_chapter(
        self,
        articles: list[Article],
        split_by: str,
        max_articles: int | None,
        max_size: int | None,
        max_tokens: int | None,
        chapter_keys: dict[uuid.UUID, uuid.UUID | None],
    ) -> list[list[Article]]:
        """Pack whole chapters into files, preferring smaller files over splitting
        a chapter across files. A chapter larger than the limit on its own is the
        only case that gets split internally (still never breaking an article).
        """
        limit = self._split_limit(split_by, max_articles, max_size, max_tokens)

        # Group consecutive articles by chapter (TOC DFS order keeps a chapter's
        # pages contiguous, so consecutive grouping == grouping by chapter).
        chapters: list[list[Article]] = []
        for article in articles:
            key = chapter_keys.get(article.id)
            if chapters and key == chapters[-1][0]:
                chapters[-1][1].append(article)  # type: ignore[index]
            else:
                chapters.append([key, [article]])  # type: ignore[list-item]
        chapter_lists = [c[1] for c in chapters]

        groups: list[list[Article]] = []
        current: list[Article] = []
        current_total = 0

        for chapter in chapter_lists:
            chapter_total = sum(self._article_metric(a, split_by) for a in chapter)

            if current and current_total + chapter_total > limit:
                groups.append(current)
                current = []
                current_total = 0

            if not current and chapter_total > limit:
                # Chapter exceeds a whole file by itself — split it internally,
                # which still guarantees individual articles stay intact.
                groups.extend(
                    self._split_articles(
                        chapter, split_by, max_articles, max_size, max_tokens
                    )
                )
                continue

            current.extend(chapter)
            current_total += chapter_total

        if current:
            groups.append(current)

        return groups

    def _split_articles(
        self,
        articles: list[Article],
        split_by: str,
        max_articles: int | None = None,
        max_size: int | None = None,
        max_tokens: int | None = None,
        respect_chapters: bool = False,
        chapter_keys: dict[uuid.UUID, uuid.UUID | None] | None = None,
    ) -> list[list[Article]]:
        """Split articles into groups without breaking individual articles.

        Guarantee: no single article is ever split across files. When
        respect_chapters is set, file boundaries also align to chapter (top-level
        TOC) boundaries — producing smaller files to keep chapters coherent.
        """
        if not articles:
            return []

        if respect_chapters and chapter_keys is not None:
            return self._split_by_chapter(
                articles, split_by, max_articles, max_size, max_tokens, chapter_keys
            )

        groups: list[list[Article]] = []
        current_group: list[Article] = []
        current_count = 0
        current_size = 0
        current_tokens = 0

        max_articles = max_articles or settings.max_articles_per_file
        max_size = max_size or settings.max_file_size_bytes
        max_tokens = max_tokens or settings.max_tokens_per_file

        for article in articles:
            would_exceed = False

            if split_by == "articles":
                would_exceed = current_count >= max_articles
            elif split_by == "size":
                would_exceed = (
                    current_size + article.content_size_bytes > max_size
                    and current_group  # never create empty group
                )
            elif split_by == "tokens":
                would_exceed = (
                    current_tokens + article.estimated_tokens > max_tokens
                    and current_group
                )

            if would_exceed:
                groups.append(current_group)
                current_group = []
                current_count = 0
                current_size = 0
                current_tokens = 0

            current_group.append(article)
            current_count += 1
            current_size += article.content_size_bytes
            current_tokens += article.estimated_tokens

        if current_group:
            groups.append(current_group)

        return groups

    # ------------------------------------------------------------------ #
    #  Image copy helper                                                   #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    #  Core generation (render pass)                                       #
    # ------------------------------------------------------------------ #

    def _generate_export(
        self,
        groups: list[list[Article]],          # plan-pass (metadata-only) groups
        source_name: str,
        source_id: uuid.UUID,
        format: str,
        load_content,                          # Callable[[list[uuid.UUID]], list[Article]]
    ) -> dict:
        """Generate export files from resolved article groups.

        *groups* contains metadata-only Article rows from the plan pass.
        *load_content* is called per render-chunk to fetch full content.
        """
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

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def export_sync(
        self,
        db: Session,
        source_id: uuid.UUID,
        article_ids: list[uuid.UUID] | None = None,
        toc_entry_ids: list[uuid.UUID] | None = None,
        topic_query: str | None = None,
        split_by: str | None = None,
        max_articles_per_file: int | None = None,
        max_file_size_bytes: int | None = None,
        max_tokens_per_file: int | None = None,
        respect_chapters: bool = False,
        format: str = "markdown",
    ) -> dict:
        """Execute an export and return metadata about the generated files.

        Plan pass: resolve articles with metadata-only columns.
        Render pass: load full content per chunk inside _generate_export.
        """
        result = db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            raise ValueError(f"Source {source_id} not found")

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

        return self._generate_export(
            groups, source.name, source_id, format, functools.partial(self._load_chunk_sync, db)
        )


# Singleton
export_engine = ExportEngine()
