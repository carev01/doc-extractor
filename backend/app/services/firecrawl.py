"""Firecrawl integration service — full-site extraction with TOC preservation."""

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.image import ArticleImage
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry


def compute_content_hash(content: str) -> str:
    """SHA-256 hex digest of markdown content used for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

logger = logging.getLogger(__name__)


class FirecrawlUnavailableError(Exception):
    """Raised when the Firecrawl service is not reachable."""
    pass


class FirecrawlService:
    """Handles documentation extraction via local Firecrawl instance."""

    # Short timeout for connectivity check so we fail fast when
    # Firecrawl is not running instead of hanging for 300s.
    CONNECT_TIMEOUT = 5.0  # seconds

    def __init__(self):
        self.base_url = settings.firecrawl_api_url.rstrip("/")
        self.api_key = settings.firecrawl_api_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.CONNECT_TIMEOUT,
                read=300.0,
                write=30.0,
                pool=30.0,
            )
        )

    async def _check_available(self) -> None:
        """Quick connectivity check — fail fast if Firecrawl is not running.

        Raises FirecrawlUnavailableError with a helpful message so the
        extraction run is marked FAILED within seconds instead of hanging
        for the full 300s read timeout.
        """
        try:
            resp = await self.client.get(
                f"{self.base_url}/", timeout=self.CONNECT_TIMEOUT
            )
            # Any response (even 404) means the server is alive.
        except httpx.ConnectError as exc:
            raise FirecrawlUnavailableError(
                f"Firecrawl is not reachable at {self.base_url}. "
                f"Ensure Firecrawl is running (e.g. via Docker Compose). "
                f"Original error: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise FirecrawlUnavailableError(
                f"Firecrawl at {self.base_url} did not respond within "
                f"{self.CONNECT_TIMEOUT}s. It may be starting up or "
                f"misconfigured. Original error: {exc}"
            ) from exc

    async def _firecrawl_map(self, url: str) -> dict[str, Any]:
        """Use Firecrawl map endpoint to discover all pages on a doc site."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await self.client.post(
            f"{self.base_url}/v1/map",
            json={"url": url, "includeSubdomains": False},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def _firecrawl_scrape(self, url: str) -> dict[str, Any]:
        """Scrape a single page via Firecrawl."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await self.client.post(
            f"{self.base_url}/v1/scrape",
            json={
                "url": url,
                "formats": ["markdown", "html"],
                "onlyMainContent": True,
            },
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def _download_image(self, img_url: str, article_dir: str) -> str | None:
        """Download an image and return the local filename."""
        try:
            resp = await self.client.get(img_url, follow_redirects=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            ext = ".png"
            if "jpeg" in content_type or "jpg" in content_type:
                ext = ".jpg"
            elif "gif" in content_type:
                ext = ".gif"
            elif "svg" in content_type:
                ext = ".svg"
            elif "webp" in content_type:
                ext = ".webp"

            filename = f"{uuid.uuid4().hex[:12]}{ext}"
            filepath = os.path.join(article_dir, filename)

            os.makedirs(article_dir, exist_ok=True)
            with open(filepath, "wb") as f:
                f.write(resp.content)

            return filename
        except Exception:
            return None

    def _parse_toc_from_urls(self, urls: list[str], base_url: str) -> list[dict]:
        """Parse a list of discovered URLs into a TOC structure.

        Heuristic: group by URL path segments, infer hierarchy from depth.
        """
        base_path = urlparse(base_url).path.rstrip("/")
        entries: list[dict] = []
        seen_paths: dict[str, dict] = {}

        for i, url in enumerate(sorted(urls)):
            parsed = urlparse(url)
            path = parsed.path.rstrip("/") or "/"

            # Remove base_path prefix
            if base_path and path.startswith(base_path):
                path = path[len(base_path):] or "/"

            segments = [s for s in path.split("/") if s]
            depth = len(segments)

            # Determine title from last segment or URL
            if segments:
                title = segments[-1].replace("-", " ").replace("_", " ").title()
            else:
                title = "Index"

            entry = {
                "title": title,
                "url": url,
                "level": depth,
                "sort_order": i,
                "is_article": True,
                "path": path,
            }
            entries.append(entry)
            seen_paths[path] = entry

        # Build parent-child relationships
        for entry in entries:
            path = entry["path"]
            segments = [s for s in path.split("/") if s]
            if len(segments) > 1:
                parent_path = "/" + "/".join(segments[:-1])
                if parent_path in seen_paths:
                    entry["parent_path"] = parent_path
                    # Mark parent as non-article (section header)
                    seen_paths[parent_path]["is_article"] = False

        return entries

    async def extract_source(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID | None = None,
    ) -> ExtractionRun:
        """Execute a full extraction for a documentation source.

        Args:
            db: Async database session.
            source_id: The documentation source to extract.
            run_id: Optional pre-existing run ID from the request scope.
                When provided, the existing ExtractionRun row is updated
                instead of creating a duplicate.
        """
        # Get source
        result = await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            raise ValueError(f"Source {source_id} not found")

        # Reuse existing run or create a new one
        if run_id is not None:
            run_result = await db.execute(
                select(ExtractionRun).where(ExtractionRun.id == run_id)
            )
            run = run_result.scalar_one_or_none()
            if run is None:
                raise ValueError(f"ExtractionRun {run_id} not found")
            # Update the pre-existing run to RUNNING status
            run.status = RunStatus.RUNNING
        else:
            run = ExtractionRun(
                source_id=source_id,
                status=RunStatus.RUNNING,
            )
            db.add(run)

        # Update source status
        source.status = SourceStatus.EXTRACTING
        await db.flush()

        try:
            # Step 0: Fast-fail if Firecrawl is not reachable
            await self._check_available()

            # Step 1: Map the site to discover all pages
            map_result = await self._firecrawl_map(source.base_url)
            discovered_urls: list[str] = map_result.get("links", [])

            # Filter to same-domain, doc-relevant URLs
            base_domain = urlparse(source.base_url).netloc
            doc_urls = [
                u for u in discovered_urls
                if urlparse(u).netloc == base_domain
                and not any(
                    skip in u.lower()
                    for skip in ["/blog/", "/tag/", "/author/", "/category/", "#"]
                )
            ]

            if not doc_urls:
                # Fallback: scrape the base URL itself
                doc_urls = [source.base_url]

            run.articles_total = len(doc_urls)

            # Step 2: Parse TOC structure
            toc_data = self._parse_toc_from_urls(doc_urls, source.base_url)

            # Step 3: Create TOC entries in DB
            toc_map: dict[str, uuid.UUID] = {}  # path -> toc_entry_id
            toc_entries_to_add: list[TOCEntry] = []

            for td in toc_data:
                toc_entry = TOCEntry(
                    source_id=source_id,
                    title=td["title"],
                    url=td["url"],
                    level=td["level"],
                    sort_order=td["sort_order"],
                    is_article=td["is_article"],
                )
                db.add(toc_entry)
                await db.flush()
                toc_map[td["path"]] = toc_entry.id
                toc_entries_to_add.append(toc_entry)

            # Set parent relationships
            for td in toc_data:
                if "parent_path" in td and td["parent_path"] in toc_map:
                    child_id = toc_map[td["path"]]
                    parent_id = toc_map[td["parent_path"]]
                    child_entry = next(
                        (e for e in toc_entries_to_add if e.id == child_id), None
                    )
                    if child_entry:
                        child_entry.parent_id = parent_id

            await db.flush()

            # Step 4: Scrape each article page
            images_dir = os.path.join(settings.export_dir, settings.images_dir)
            extracted_count = 0
            unchanged_count = 0
            updated_count = 0

            for i, url in enumerate(doc_urls):
                try:
                    scrape_result = await self._firecrawl_scrape(url)
                    markdown_content = scrape_result.get("data", {}).get("markdown", "")
                    html_content = scrape_result.get("data", {}).get("html", "")
                    metadata = scrape_result.get("data", {}).get("metadata", {})

                    if not markdown_content.strip():
                        continue

                    # Compute hash of the RAW scraped markdown (before any
                    # image-reference rewriting) so the value is stable across
                    # runs and is the basis for incremental change detection.
                    content_hash = compute_content_hash(markdown_content)

                    # Incremental check: does an article already exist for
                    # this source + URL?
                    existing_result = await db.execute(
                        select(Article).where(
                            Article.source_id == source_id,
                            Article.source_url == url,
                        )
                    )
                    existing_article = existing_result.scalar_one_or_none()

                    if (
                        existing_article is not None
                        and existing_article.content_hash == content_hash
                    ):
                        # Content unchanged → skip (no DB write, no image
                        # re-download).
                        unchanged_count += 1
                        continue

                    # Determine title
                    title = metadata.get("title") or metadata.get("og:title") or url
                    # Clean title
                    title = title.split("|")[0].split("—")[0].strip()

                    # Parse last-updated timestamp
                    last_updated = None
                    if metadata.get("lastModified"):
                        try:
                            last_updated = datetime.fromisoformat(
                                metadata["lastModified"].replace("Z", "+00:00")
                            )
                        except (ValueError, TypeError):
                            pass

                    # Find TOC entry for this URL
                    parsed_path = urlparse(url).path.rstrip("/") or "/"
                    base_path = urlparse(source.base_url).path.rstrip("/")
                    if base_path and parsed_path.startswith(base_path):
                        parsed_path = parsed_path[len(base_path):] or "/"

                    toc_entry_id = toc_map.get(parsed_path)

                    # Estimate tokens (rough: 1 token ≈ 4 chars)
                    estimated_tokens = len(markdown_content) // 4
                    content_size = len(markdown_content.encode("utf-8"))

                    if existing_article is not None:
                        # Content changed → snapshot the OLD content into an
                        # ArticleVersion row before overwriting, then update.
                        version = ArticleVersion(
                            article_id=existing_article.id,
                            extraction_run_id=run.id,
                            content_markdown=existing_article.content_markdown,
                            content_hash=existing_article.content_hash,
                        )
                        db.add(version)

                        article = existing_article
                        article.extraction_run_id = run.id
                        article.toc_entry_id = toc_entry_id
                        article.title = title
                        article.source_url = url
                        article.content_markdown = markdown_content
                        article.content_html = html_content
                        article.content_hash = content_hash
                        article.last_updated_at = (
                            last_updated or datetime.now(timezone.utc)
                        )
                        article.sort_order = i
                        article.estimated_tokens = estimated_tokens
                        article.content_size_bytes = content_size
                        # Drop stale image records; they will be re-downloaded.
                        for old_img in list(article.images):
                            await db.delete(old_img)
                        await db.flush()
                        updated_count += 1
                    else:
                        article = Article(
                            source_id=source_id,
                            extraction_run_id=run.id,
                            toc_entry_id=toc_entry_id,
                            title=title,
                            source_url=url,
                            content_markdown=markdown_content,
                            content_html=html_content,
                            content_hash=content_hash,
                            last_updated_at=last_updated,
                            sort_order=i,
                            estimated_tokens=estimated_tokens,
                            content_size_bytes=content_size,
                        )
                        db.add(article)
                        await db.flush()
                        extracted_count += 1

                    # Step 5: Download linked images
                    if html_content:
                        soup = BeautifulSoup(html_content, "html.parser")
                        img_tags = soup.find_all("img")
                        article_img_dir = os.path.join(images_dir, str(article.id))

                        for j, img in enumerate(img_tags):
                            src = img.get("src", "")
                            if not src:
                                continue

                            # Resolve relative URLs
                            full_src = urljoin(url, src)

                            # Only download same-domain or common CDN images
                            if not full_src.startswith(("http://", "https://")):
                                continue

                            local_filename = await self._download_image(
                                full_src, article_img_dir
                            )
                            if local_filename:
                                local_path = os.path.join(
                                    settings.images_dir, str(article.id), local_filename
                                )
                                img_record = ArticleImage(
                                    article_id=article.id,
                                    original_url=full_src,
                                    local_filename=local_filename,
                                    local_path=local_path,
                                    alt_text=img.get("alt", ""),
                                    sort_order=j,
                                )
                                db.add(img_record)

                                # Update markdown image references
                                markdown_content = markdown_content.replace(
                                    full_src, local_path
                                )
                                # Also try relative paths
                                markdown_content = markdown_content.replace(
                                    src, local_path
                                )

                    # Update article with corrected image paths. Note:
                    # content_hash deliberately stays the hash of the raw
                    # markdown so future runs compare like-for-like.
                    article.content_markdown = markdown_content

                    run.articles_extracted = extracted_count
                    run.articles_updated = updated_count
                    run.articles_unchanged = unchanged_count

                    # Commit periodically
                    if (extracted_count + updated_count) % 10 == 0:
                        await db.flush()

                    # Small delay to be polite to the target server
                    await asyncio.sleep(0.5)

                except Exception as e:
                    # Log but continue with next article
                    logger.warning("Error scraping %s: %s", url, e)
                    continue

            # Mark run as completed
            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            run.articles_extracted = extracted_count
            run.articles_updated = updated_count
            run.articles_unchanged = unchanged_count

            source.status = SourceStatus.COMPLETED
            source.last_extracted_at = datetime.now(timezone.utc)

            await db.flush()
            return run

        except FirecrawlUnavailableError as e:
            # Firecrawl not running — mark run as FAILED immediately
            logger.error("Firecrawl unavailable: %s", e)
            run.status = RunStatus.FAILED
            run.error_message = str(e)[:4096]
            run.completed_at = datetime.now(timezone.utc)
            source.status = SourceStatus.FAILED
            source.error_message = str(e)[:4096]
            await db.flush()
            raise

        except Exception as e:
            # Mark as failed
            logger.exception("Extraction failed for source %s", source_id)
            run.status = RunStatus.FAILED
            run.error_message = str(e)[:4096]
            run.completed_at = datetime.now(timezone.utc)
            source.status = SourceStatus.FAILED
            source.error_message = str(e)[:4096]
            await db.flush()
            raise

    async def close(self):
        await self.client.aclose()


# Singleton
firecrawl_service = FirecrawlService()