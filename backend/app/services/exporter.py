"""Markdown export engine — full, partial, and split exports."""

import os
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models.article import Article
from app.models.image import ArticleImage
from app.models.source import DocumentationSource
from app.models.toc import TOCEntry


class ExportEngine:
    """Builds markdown export files from extracted articles."""

    def __init__(self):
        self.export_dir = os.path.abspath(settings.export_dir)
        os.makedirs(self.export_dir, exist_ok=True)

    async def _resolve_articles(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        article_ids: list[uuid.UUID] | None = None,
        toc_entry_ids: list[uuid.UUID] | None = None,
        topic_query: str | None = None,
    ) -> list[Article]:
        """Resolve which articles to export based on selection criteria."""
        query = (
            select(Article)
            .where(Article.source_id == source_id)
            .options(selectinload(Article.images), selectinload(Article.toc_entry))
        )

        if article_ids:
            query = query.where(Article.id.in_(article_ids))
        elif toc_entry_ids:
            # Get all articles under these TOC entries (including children)
            toc_ids_set = set(toc_entry_ids)

            # Expand: get all descendant TOC entries
            all_toc = await db.execute(
                select(TOCEntry).where(TOCEntry.source_id == source_id)
            )
            toc_entries = all_toc.scalars().all()

            # Build parent->children map
            children_map: dict[uuid.UUID, list[uuid.UUID]] = {}
            for te in toc_entries:
                if te.parent_id:
                    children_map.setdefault(te.parent_id, []).append(te.id)

            # Expand toc_entry_ids to include all descendants
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
            # Full-text search on content
            query = query.where(
                Article.content_markdown.ilike(f"%{topic_query}%")
            )

        # Always order by TOC sort_order, then article sort_order
        query = query.order_by(Article.sort_order)

        result = await db.execute(query)
        return list(result.scalars().all())

    def _build_markdown_document(
        self, articles: Sequence[Article], source_name: str
    ) -> str:
        """Build a single markdown document from a list of articles."""
        lines: list[str] = []

        # Document header
        lines.append(f"# {source_name}")
        lines.append("")
        lines.append(
            f"> Extracted: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        lines.append(f"> Articles: {len(articles)}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Table of Contents
        lines.append("## Table of Contents")
        lines.append("")
        for i, article in enumerate(articles, 1):
            lines.append(f"{i}. [{article.title}](#{self._slugify(article.title)})")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Articles
        for article in articles:
            lines.append(f"## {article.title}")
            lines.append("")
            lines.append(f"**Source:** [{article.source_url}]({article.source_url})")
            if article.last_updated_at:
                lines.append(
                    f"**Last Updated:** {article.last_updated_at.strftime('%Y-%m-%d %H:%M UTC')}"
                )
            lines.append(f"**Extracted:** {article.extracted_at.strftime('%Y-%m-%d %H:%M UTC')}")
            lines.append("")

            # Article content
            lines.append(article.content_markdown)
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def _slugify(self, text: str) -> str:
        """Create a GitHub-flavored markdown anchor slug."""
        slug = text.lower().strip()
        slug = "".join(c if c.isalnum() or c in " -_" else "" for c in slug)
        slug = slug.replace(" ", "-")
        # Collapse multiple dashes
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug.strip("-")

    def _split_articles(
        self,
        articles: list[Article],
        split_by: str,
        max_articles: int | None = None,
        max_size: int | None = None,
        max_tokens: int | None = None,
    ) -> list[list[Article]]:
        """Split articles into groups without breaking individual articles.

        Guarantee: no single article is ever split across files.
        """
        if not articles:
            return []

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

    async def export(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        article_ids: list[uuid.UUID] | None = None,
        toc_entry_ids: list[uuid.UUID] | None = None,
        topic_query: str | None = None,
        split_by: str | None = None,
        max_articles_per_file: int | None = None,
        max_file_size_bytes: int | None = None,
        max_tokens_per_file: int | None = None,
    ) -> dict:
        """Execute an export and return metadata about the generated files."""
        # Get source
        result = await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            raise ValueError(f"Source {source_id} not found")

        # Resolve articles
        articles = await self._resolve_articles(
            db, source_id, article_ids, toc_entry_ids, topic_query
        )

        if not articles:
            raise ValueError("No articles matched the selection criteria")

        return self._generate_export(articles, source.name, source_id, split_by, max_articles_per_file, max_file_size_bytes, max_tokens_per_file)

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
    ) -> dict:
        """Synchronous version of export for testing."""
        result = db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            raise ValueError(f"Source {source_id} not found")

        articles = self._resolve_articles_sync(
            db, source_id, article_ids, toc_entry_ids, topic_query
        )

        if not articles:
            raise ValueError("No articles matched the selection criteria")

        return self._generate_export(articles, source.name, source_id, split_by, max_articles_per_file, max_file_size_bytes, max_tokens_per_file)

    def _resolve_articles_sync(
        self,
        db: Session,
        source_id: uuid.UUID,
        article_ids: list[uuid.UUID] | None = None,
        toc_entry_ids: list[uuid.UUID] | None = None,
        topic_query: str | None = None,
    ) -> list[Article]:
        """Synchronous article resolution."""
        query = (
            select(Article)
            .where(Article.source_id == source_id)
            .options(selectinload(Article.images), selectinload(Article.toc_entry))
        )

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
                Article.content_markdown.ilike(f"%{topic_query}%")
            )

        query = query.order_by(Article.sort_order)
        result = db.execute(query)
        return list(result.scalars().all())

    def _generate_export(
        self,
        articles: list[Article],
        source_name: str,
        source_id: uuid.UUID,
        split_by: str | None = None,
        max_articles_per_file: int | None = None,
        max_file_size_bytes: int | None = None,
        max_tokens_per_file: int | None = None,
    ) -> dict:
        """Generate export files from resolved articles."""
        if split_by:
            groups = self._split_articles(
                articles,
                split_by,
                max_articles_per_file,
                max_file_size_bytes,
                max_tokens_per_file,
            )
        else:
            groups = [articles]

        export_id = uuid.uuid4()
        export_subdir = os.path.join(self.export_dir, str(export_id))
        os.makedirs(export_subdir, exist_ok=True)

        files_info: list[dict] = []
        total_size = 0

        for i, group in enumerate(groups, 1):
            if len(groups) == 1:
                filename = f"{source_name.replace(' ', '_')}.md"
            else:
                filename = f"{source_name.replace(' ', '_')}_part{i:03d}.md"

            content = self._build_markdown_document(group, source_name)
            filepath = os.path.join(export_subdir, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

            file_size = len(content.encode("utf-8"))
            total_size += file_size
            group_tokens = sum(a.estimated_tokens for a in group)

            files_info.append({
                "filename": filename,
                "article_count": len(group),
                "size_bytes": file_size,
                "estimated_tokens": group_tokens,
                "first_article_title": group[0].title,
                "last_article_title": group[-1].title,
            })

        return {
            "export_id": export_id,
            "source_id": source_id,
            "file_count": len(files_info),
            "total_articles": len(articles),
            "total_size_bytes": total_size,
            "files": files_info,
        }


# Singleton
export_engine = ExportEngine()
