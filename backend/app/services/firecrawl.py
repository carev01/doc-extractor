"""Firecrawl integration service — full-site extraction with TOC preservation."""

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
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

    CONNECT_TIMEOUT = 5.0

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
        """Quick connectivity check — fail fast if Firecrawl is not running."""
        try:
            await self.client.get(f"{self.base_url}/", timeout=self.CONNECT_TIMEOUT)
        except httpx.ConnectError as exc:
            raise FirecrawlUnavailableError(
                f"Firecrawl is not reachable at {self.base_url}. "
                f"Ensure Firecrawl is running. Original error: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise FirecrawlUnavailableError(
                f"Firecrawl at {self.base_url} did not respond within "
                f"{self.CONNECT_TIMEOUT}s. Original error: {exc}"
            ) from exc

    async def _firecrawl_request(self, url: str, payload: dict) -> dict:
        """Make a Firecrawl v2 scrape request and return the data dict."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await self.client.post(
            f"{self.base_url}/v2/scrape",
            json={"url": url, **payload},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def _scrape_nav_html(self, url: str) -> str:
        """Fetch full page HTML for nav parsing during TOC discovery.

        onlyMainContent=False is required to preserve the left nav structure
        that only appears in the full page render.
        """
        data = await self._firecrawl_request(url, {
            "formats": ["html"],
            "onlyMainContent": False,
            "waitFor": 3000,
        })
        return data.get("html", "")

    async def _scrape_article(self, url: str) -> tuple[str, str]:
        """Fetch article content as (markdown, html) scoped to the #doc element.

        includeTags=["#doc"] instructs Firecrawl to discard everything outside
        the main article container before converting, so the returned markdown
        contains only article content — no nav, no feedback widget, no TOC.
        The HTML is filtered the same way and is only used to extract image
        URLs and the last-updated timestamp.
        """
        data = await self._firecrawl_request(url, {
            "formats": ["markdown", "html"],
            "onlyMainContent": False,
            "includeTags": ["#doc"],
            "waitFor": 3000,
        })
        return data.get("markdown", ""), data.get("html", "")

    def _parse_nav_items(self, ul_el) -> list[dict]:
        """Extract ordered {title, url, is_parent} items from a nav <ul>.

        Preserves DOM order. is_parent=True when data-is-parent is present,
        meaning children are only revealed when that page is scraped directly.
        """
        items = []
        for li in ul_el.find_all("li", class_="nav-row", recursive=False):
            div = li.find(class_="nav-item")
            if not div:
                continue
            a_tag = div.find("a")
            if not a_tag:
                continue
            href = a_tag.get("href", "").strip()
            title = a_tag.get_text(strip=True)
            is_parent = div.has_attr("data-is-parent")
            if href and title:
                items.append({"title": title, "url": href, "is_parent": is_parent})
        return items

    async def _build_toc_recursive(
        self,
        url: str,
        level: int,
        visited: set[str],
        nav_html_cache: dict[str, str],
    ) -> list[dict]:
        """Build an ordered TOC list via depth-first recursive nav scraping.

        Each page's nav only reveals the active item's direct children, so
        parent pages must be scraped individually to discover their children.

        - level=0 (root URL): reads from <ul class="nav-group-root">
        - level>0: reads the children of the active nav item on that page
        """
        if url in visited:
            return []
        visited.add(url)

        if url not in nav_html_cache:
            try:
                nav_html_cache[url] = await self._scrape_nav_html(url)
            except Exception as exc:
                logger.warning("TOC scrape failed for %s: %s", url, exc)
                return []

        html = nav_html_cache[url]
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        nav = soup.find(id="nav")
        if not nav:
            return []

        if level == 0:
            root_ul = nav.find("ul", class_="nav-group-root")
            if not root_ul:
                root_ul = nav.find("ul", class_="nav-group")
            if not root_ul:
                return []
            items = self._parse_nav_items(root_ul)
        else:
            active_div = nav.find(class_="nav-item-active")
            if not active_div:
                return []
            parent_li = active_div.parent  # <li class="nav-row">
            children_ul = parent_li.find("ul", class_="nav-group")
            if not children_ul:
                return []
            items = self._parse_nav_items(children_ul)

        toc: list[dict] = []
        for item in items:
            toc.append({
                "title": item["title"],
                "url": item["url"],
                "level": level,
                "is_article": True,
            })
            if item["is_parent"]:
                children = await self._build_toc_recursive(
                    item["url"], level + 1, visited, nav_html_cache
                )
                toc.extend(children)

        return toc

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

    async def extract_source(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
    ) -> ExtractionRun:
        """Execute a full extraction for a documentation source.

        Phase 1 — TOC discovery: recursively scrapes parent nav items in DOM
        order to build a complete depth-first ordered TOC.

        Phase 2 — Content scraping: calls Firecrawl with includeTags=["#doc"]
        so it delivers clean article markdown directly, scoped to the main
        content container with nav/feedback/anchor-TOC stripped server-side.
        """
        result = await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            raise ValueError(f"Source {source_id} not found")

        if run_id is not None:
            run_result = await db.execute(
                select(ExtractionRun).where(ExtractionRun.id == run_id)
            )
            run = run_result.scalar_one_or_none()
            if run is None:
                raise ValueError(f"ExtractionRun {run_id} not found")
            run.status = RunStatus.RUNNING
        else:
            run = ExtractionRun(source_id=source_id, status=RunStatus.RUNNING)
            db.add(run)

        source.status = SourceStatus.EXTRACTING
        await db.flush()

        try:
            await self._check_available()

            # ── Phase 1: Build ordered TOC via recursive nav scraping ──────
            logger.info("Discovering TOC for %s", source.base_url)
            nav_html_cache: dict[str, str] = {}
            visited: set[str] = set()

            toc_entries = await self._build_toc_recursive(
                source.base_url, level=0, visited=visited,
                nav_html_cache=nav_html_cache,
            )

            if not toc_entries:
                toc_entries = [{
                    "title": "Index",
                    "url": source.base_url,
                    "level": 0,
                    "is_article": True,
                }]

            # Deduplicate while preserving DFS order
            seen_toc_urls: set[str] = set()
            unique_entries: list[dict] = []
            for entry in toc_entries:
                if entry["url"] not in seen_toc_urls:
                    seen_toc_urls.add(entry["url"])
                    entry["sort_order"] = len(unique_entries)
                    unique_entries.append(entry)
            toc_entries = unique_entries

            logger.info("TOC contains %d pages", len(toc_entries))
            run.articles_total = len(toc_entries)

            # ── Persist TOC entries with parent-child relationships ─────────
            toc_db_map: dict[str, uuid.UUID] = {}
            level_to_parent: dict[int, uuid.UUID] = {}

            for td in toc_entries:
                parent_id = (
                    level_to_parent.get(td["level"] - 1)
                    if td["level"] > 0
                    else None
                )
                toc_entry = TOCEntry(
                    source_id=source_id,
                    title=td["title"],
                    url=td["url"],
                    level=td["level"],
                    sort_order=td["sort_order"],
                    is_article=td["is_article"],
                    parent_id=parent_id,
                )
                db.add(toc_entry)
                await db.flush()

                toc_db_map[td["url"]] = toc_entry.id
                level_to_parent[td["level"]] = toc_entry.id
                for deeper in [k for k in level_to_parent if k > td["level"]]:
                    del level_to_parent[deeper]

            await db.flush()

            # ── Phase 2: Scrape content for each page in TOC order ─────────
            images_dir = os.path.join(settings.export_dir, settings.images_dir)
            extracted_count = 0
            unchanged_count = 0
            updated_count = 0

            for i, entry in enumerate(toc_entries):
                url = entry["url"]
                try:
                    # Firecrawl returns clean markdown scoped to #doc — no
                    # BeautifulSoup/markdownify conversion needed on our side.
                    markdown_content, doc_html = await self._scrape_article(url)

                    if not markdown_content.strip():
                        logger.debug("No content from %s — skipping", url)
                        continue

                    content_hash = compute_content_hash(markdown_content)

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
                        unchanged_count += 1
                        run.articles_unchanged = unchanged_count
                        continue

                    # Parse last-updated from the filtered #doc HTML
                    last_updated = None
                    if doc_html:
                        doc_soup = BeautifulSoup(doc_html, "html.parser")
                        time_tag = doc_soup.find("time", attrs={"datetime": True})
                        if time_tag:
                            try:
                                last_updated = datetime.fromisoformat(
                                    time_tag["datetime"].replace("Z", "+00:00")
                                )
                            except (ValueError, TypeError):
                                pass

                    toc_entry_id = toc_db_map.get(url)
                    estimated_tokens = len(markdown_content) // 4
                    content_size = len(markdown_content.encode("utf-8"))
                    title = entry["title"]

                    if existing_article is not None:
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
                        article.content_html = doc_html
                        article.content_hash = content_hash
                        article.last_updated_at = (
                            last_updated or datetime.now(timezone.utc)
                        )
                        article.sort_order = i
                        article.estimated_tokens = estimated_tokens
                        article.content_size_bytes = content_size
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
                            content_html=doc_html,
                            content_hash=content_hash,
                            last_updated_at=last_updated,
                            sort_order=i,
                            estimated_tokens=estimated_tokens,
                            content_size_bytes=content_size,
                        )
                        db.add(article)
                        await db.flush()
                        extracted_count += 1

                    # Download images referenced in the article
                    if doc_html:
                        img_soup = BeautifulSoup(doc_html, "html.parser")
                        article_img_dir = os.path.join(images_dir, str(article.id))

                        for j, img in enumerate(img_soup.find_all("img")):
                            src = img.get("src", "")
                            if not src:
                                continue
                            full_src = urljoin(url, src)
                            if not full_src.startswith(("http://", "https://")):
                                continue

                            local_filename = await self._download_image(
                                full_src, article_img_dir
                            )
                            if local_filename:
                                local_path = os.path.join(
                                    settings.images_dir, str(article.id), local_filename
                                )
                                db.add(ArticleImage(
                                    article_id=article.id,
                                    original_url=full_src,
                                    local_filename=local_filename,
                                    local_path=local_path,
                                    alt_text=img.get("alt", ""),
                                    sort_order=j,
                                ))
                                markdown_content = markdown_content.replace(
                                    full_src, local_path
                                )
                                markdown_content = markdown_content.replace(
                                    src, local_path
                                )

                    article.content_markdown = markdown_content

                    run.articles_extracted = extracted_count
                    run.articles_updated = updated_count
                    run.articles_unchanged = unchanged_count

                    if (extracted_count + updated_count) % 10 == 0:
                        await db.flush()

                    await asyncio.sleep(0.5)

                except Exception as exc:
                    logger.warning("Error scraping %s: %s", url, exc)
                    continue

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            run.articles_extracted = extracted_count
            run.articles_updated = updated_count
            run.articles_unchanged = unchanged_count

            source.status = SourceStatus.COMPLETED
            source.last_extracted_at = datetime.now(timezone.utc)

            await db.flush()
            return run

        except FirecrawlUnavailableError as exc:
            logger.error("Firecrawl unavailable: %s", exc)
            run.status = RunStatus.FAILED
            run.error_message = str(exc)[:4096]
            run.completed_at = datetime.now(timezone.utc)
            source.status = SourceStatus.FAILED
            source.error_message = str(exc)[:4096]
            await db.flush()
            raise

        except Exception as exc:
            logger.exception("Extraction failed for source %s", source_id)
            run.status = RunStatus.FAILED
            run.error_message = str(exc)[:4096]
            run.completed_at = datetime.now(timezone.utc)
            source.status = SourceStatus.FAILED
            source.error_message = str(exc)[:4096]
            await db.flush()
            raise

    async def close(self):
        await self.client.aclose()


# Singleton
firecrawl_service = FirecrawlService()
