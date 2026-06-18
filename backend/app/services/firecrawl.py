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
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.image import ArticleImage
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.services.profiles import registry as profile_registry
from app.services.profiles.detector import detect_platform
from app.services.profiles.scraper import Scraper

# Default content scrape options when no profile config is supplied (legacy Commvault).
_LEGACY_CONTENT = {"includeTags": ["#doc"], "onlyMainContent": False, "waitFor": 1500}


def compute_content_hash(content: str) -> str:
    """SHA-256 hex digest of markdown content used for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


logger = logging.getLogger(__name__)


def _dedupe_toc_entries(entries: list[dict]) -> list[dict]:
    """Assign sort_order and drop duplicate *article* URLs, preserving DFS order.

    Entries with a falsy url (structural section headers) are always kept and
    never collapsed."""
    seen: set[str] = set()
    out: list[dict] = []
    for e in entries:
        u = e.get("url") or ""
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        e["sort_order"] = len(out)
        out.append(e)
    return out


class FirecrawlUnavailableError(Exception):
    """Raised when the Firecrawl service is not reachable."""
    pass


class FirecrawlService:
    """Handles documentation extraction via local Firecrawl instance."""

    CONNECT_TIMEOUT = 5.0
    EMPTY_CONTENT_RETRIES = 2
    EMPTY_CONTENT_RETRY_DELAY = 2.0
    BATCH_POLL_INTERVAL = 5.0

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
        # Per-source content scrape options, set at run start so the webhook /
        # empty-content retries scope content the same way the batch did.
        self._content_config_by_source: dict[uuid.UUID, dict] = {}

    async def _resolve_profile(self, source: DocumentationSource):
        """Pick the extraction profile for a source.

        Resolution order:
        1. Stored ``source.platform`` override — if the name resolves to a
           registered profile, use it immediately.
        2. Auto-detection — scrape the root URL once and iterate registered
           profiles' ``detect()`` methods.  If a match is found, store it on
           ``source.platform`` so the caller can persist it with a DB commit.
        3. Default — fall back to the Commvault profile (preserves prior
           behaviour until a generic fallback profile is implemented).
        """
        if source.platform:
            p = profile_registry.get(source.platform)
            if p is not None:
                return p

        # Auto-detect: scrape the root page once and check all profiles.
        try:
            scraper = Scraper(self)
            root_html = await scraper.get_html(source.base_url)
            detected_name = detect_platform(root_html, source.base_url)
            if detected_name:
                p = profile_registry.get(detected_name)
                if p is not None:
                    source.platform = detected_name  # caller commits
                    logger.info(
                        "Auto-detected platform '%s' for %s",
                        detected_name,
                        source.base_url,
                    )
                    return p
        except Exception as exc:
            logger.warning(
                "Platform auto-detection failed for %s: %s", source.base_url, exc
            )

        return profile_registry.get("commvault")

    def _auth_headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    async def _check_available(self) -> None:
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
        resp = await self.client.post(
            f"{self.base_url}/v2/scrape",
            json={"url": url, **payload},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def _scrape_article(
        self, url: str, tag: str | None = None, content_config: dict | None = None
    ) -> tuple[str, str, str | None, str | None]:
        """Return (markdown, html, change_status, diff_text) scoped to the #doc element.

        change_status is the Firecrawl changeTracking status ("new"|"same"|"changed"|
        "removed") when an API key is configured; None otherwise.
        diff_text is the git-diff string when change_status is "changed".
        """
        formats: list = ["markdown", "html"]
        if self.api_key:
            # The changeTracking baseline tag lives inside the changeTracking
            # format object in Firecrawl's v2 API (a top-level "tag" is rejected).
            ct_format: dict = {"type": "changeTracking", "modes": ["git-diff"]}
            if tag:
                ct_format["tag"] = tag
            formats.append(ct_format)
        payload: dict = {"formats": formats, **(content_config or _LEGACY_CONTENT)}
        data = await self._firecrawl_request(url, payload)
        markdown = data.get("markdown", "")
        html = data.get("html", "")
        ct = data.get("changeTracking") or {}
        change_status = ct.get("changeStatus")
        diff_text = (ct.get("diff") or {}).get("text")
        return markdown, html, change_status, diff_text

    async def _scrape_article_with_retry(
        self, url: str, tag: str | None = None, content_config: dict | None = None
    ) -> tuple[str, str, str | None, str | None]:
        """Scrape a single article, retrying on empty-content responses."""
        markdown, html, change_status, diff_text = await self._scrape_article(
            url, tag=tag, content_config=content_config
        )
        for attempt in range(self.EMPTY_CONTENT_RETRIES):
            if markdown.strip():
                return markdown, html, change_status, diff_text
            logger.warning(
                "Empty content from %s (attempt %d/%d) — retrying in %.0fs",
                url, attempt + 1, self.EMPTY_CONTENT_RETRIES,
                self.EMPTY_CONTENT_RETRY_DELAY,
            )
            await asyncio.sleep(self.EMPTY_CONTENT_RETRY_DELAY)
            markdown, html, change_status, diff_text = await self._scrape_article(
                url, tag=tag, content_config=content_config
            )
        return markdown, html, change_status, diff_text

    async def _download_image(self, img_url: str, article_dir: str) -> str | None:
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

    async def process_article_result(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        url: str,
        markdown_content: str,
        doc_html: str,
        toc_entry_id: uuid.UUID | None,
        sort_order: int,
        title: str,
        change_status: str | None = None,
        diff_text: str | None = None,
    ) -> str:
        """Store or skip a single article and atomically increment run counters.

        Returns "new" | "updated" | "unchanged" | "empty".
        Used by both the inline polling path and the webhook handler so all
        article processing is consolidated here.

        change_status is the Firecrawl changeTracking verdict ("same"|"new"|"changed"|
        "removed"). When "same", the DB write is skipped entirely — no hash needed.
        When "new" (first run with changeTracking for this tag) or None (no API key),
        we fall back to hash comparison so we don't create spurious ArticleVersions
        for articles that haven't actually changed since the last extraction.
        """
        if not markdown_content.strip():
            logger.warning("Empty content from %s — skipping", url)
            return "empty"

        # Fast-path: Firecrawl has a prior snapshot and confirms no change.
        # Content is untouched, but we still scraped the page this run — bump
        # extracted_at so it reflects the last scrape, not the last change.
        if change_status == "same":
            # The TOC is deleted and rebuilt every run (new entry ids), so the
            # article's toc_entry_id was just NULLed by SET NULL. Re-link it (and
            # refresh the TOC-derived sort_order/title) even though the content is
            # unchanged — otherwise the page orphans and the browser hides it.
            await db.execute(
                update(Article)
                .where(Article.source_id == source_id, Article.source_url == url)
                .values(
                    extracted_at=datetime.now(timezone.utc),
                    toc_entry_id=toc_entry_id,
                    sort_order=sort_order,
                    title=title,
                )
            )
            await db.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .values(articles_unchanged=ExtractionRun.articles_unchanged + 1)
            )
            await db.commit()
            return "unchanged"

        content_hash = compute_content_hash(markdown_content)

        existing_result = await db.execute(
            select(Article).where(
                Article.source_id == source_id,
                Article.source_url == url,
            )
        )
        existing_article = existing_result.scalar_one_or_none()

        # For "new" or None change_status fall back to hash comparison. "new" happens
        # on the first extraction after changeTracking was enabled (Firecrawl has no
        # prior snapshot for this tag yet, but our DB may already have the article).
        if change_status in (None, "new"):
            if existing_article is not None and existing_article.content_hash == content_hash:
                # Unchanged content, but scraped this run — record the scrape time
                # and re-link to the freshly-rebuilt TOC entry (the prior link was
                # NULLed when the TOC was rebuilt) so the page isn't orphaned.
                existing_article.extracted_at = datetime.now(timezone.utc)
                existing_article.toc_entry_id = toc_entry_id
                existing_article.sort_order = sort_order
                existing_article.title = title
                await db.execute(
                    update(ExtractionRun)
                    .where(ExtractionRun.id == run_id)
                    .values(articles_unchanged=ExtractionRun.articles_unchanged + 1)
                )
                await db.commit()
                return "unchanged"

        # Parse last-updated timestamp from the filtered #doc HTML
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

        media_root = os.path.abspath(settings.media_dir)
        estimated_tokens = len(markdown_content) // 4
        content_size = len(markdown_content.encode("utf-8"))

        if existing_article is not None:
            version = ArticleVersion(
                article_id=existing_article.id,
                extraction_run_id=run_id,
                content_markdown=existing_article.content_markdown,
                content_hash=existing_article.content_hash,
                diff_text=diff_text,
            )
            db.add(version)

            article = existing_article
            article.extraction_run_id = run_id
            article.toc_entry_id = toc_entry_id
            article.title = title
            article.source_url = url
            article.content_markdown = markdown_content
            article.content_html = doc_html
            article.content_hash = content_hash
            # Source's own update time — left NULL when the page exposes none,
            # rather than masking it with the scrape time.
            article.last_updated_at = last_updated
            # extracted_at tracks the last scrape; created_at stays first-seen.
            article.extracted_at = datetime.now(timezone.utc)
            article.sort_order = sort_order
            article.estimated_tokens = estimated_tokens
            article.content_size_bytes = content_size

            old_imgs = await db.execute(
                select(ArticleImage).where(ArticleImage.article_id == existing_article.id)
            )
            for old_img in old_imgs.scalars():
                await db.delete(old_img)
            await db.flush()
            outcome = "updated"
        else:
            article = Article(
                source_id=source_id,
                extraction_run_id=run_id,
                created_run_id=run_id,
                toc_entry_id=toc_entry_id,
                title=title,
                source_url=url,
                content_markdown=markdown_content,
                content_html=doc_html,
                content_hash=content_hash,
                last_updated_at=last_updated,
                sort_order=sort_order,
                estimated_tokens=estimated_tokens,
                content_size_bytes=content_size,
            )
            db.add(article)
            await db.flush()
            outcome = "new"

        # Download images and rewrite their references in the markdown to the
        # served /media URL so the frontend renders them directly and exports
        # can rewrite to relative paths. Images are parsed from the HTML format
        # (the markdown only carries URLs), so no extra Firecrawl scan is needed.
        if doc_html:
            img_soup = BeautifulSoup(doc_html, "html.parser")
            article_img_dir = os.path.join(media_root, str(article.id))

            for j, img in enumerate(img_soup.find_all("img")):
                src = img.get("src", "")
                if not src:
                    continue
                full_src = urljoin(url, src)
                if not full_src.startswith(("http://", "https://")):
                    continue

                local_filename = await self._download_image(full_src, article_img_dir)
                if local_filename:
                    served_url = (
                        f"{settings.media_url_prefix}/{article.id}/{local_filename}"
                    )
                    db.add(ArticleImage(
                        article_id=article.id,
                        original_url=full_src,
                        local_filename=local_filename,
                        local_path=served_url,
                        alt_text=img.get("alt", ""),
                        sort_order=j,
                    ))
                    # Replace the resolved absolute URL first. Only fall back to
                    # the raw src for non-trivial relative paths, to avoid a
                    # blind substring replace clobbering short, ambiguous tokens.
                    markdown_content = markdown_content.replace(full_src, served_url)
                    if src != full_src and src.startswith(("/", "./", "../")):
                        markdown_content = markdown_content.replace(src, served_url)

        article.content_markdown = markdown_content

        # Atomic counter increment so concurrent webhook calls don't race.
        if outcome == "new":
            await db.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .values(articles_extracted=ExtractionRun.articles_extracted + 1)
            )
        else:
            await db.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .values(articles_updated=ExtractionRun.articles_updated + 1)
            )
        await db.commit()
        return outcome

    async def _submit_batch(
        self, urls: list[str], source_id: uuid.UUID, content_config: dict | None = None
    ) -> str:
        """Submit a batch scrape job and return the Firecrawl job ID."""
        formats: list = ["markdown", "html"]
        if self.api_key:
            # The changeTracking baseline tag lives inside the changeTracking
            # format object in Firecrawl's v2 API (a top-level "tag" is rejected).
            formats.append({
                "type": "changeTracking",
                "modes": ["git-diff"],
                "tag": f"src-{source_id}",
            })
        payload: dict = {"urls": urls, "formats": formats, **(content_config or _LEGACY_CONTENT)}
        resp = await self.client.post(
            f"{self.base_url}/v2/batch/scrape",
            json=payload,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        job_id = resp.json()["id"]
        logger.info("Batch job submitted: %s (%d URLs)", job_id, len(urls))
        return job_id

    async def _get_batch_status(self, url: str) -> dict:
        """GET a batch status page (accepts both full URL and bare job ID)."""
        if not url.startswith("http"):
            url = f"{self.base_url}/v2/batch/scrape/{url}"
        resp = await self.client.get(url, headers=self._auth_headers())
        resp.raise_for_status()
        return resp.json()

    async def _wait_for_batch_completion(self, job_id: str) -> None:
        """Poll batch status until the job finishes (webhook mode — results handled elsewhere)."""
        while True:
            data = await self._get_batch_status(job_id)
            status = data.get("status", "")
            logger.info(
                "Batch %s: %s (%d/%d)",
                job_id, status, data.get("completed", 0), data.get("total", 0),
            )
            if status in ("completed", "failed"):
                return
            await asyncio.sleep(self.BATCH_POLL_INTERVAL)

    async def _poll_batch_and_process(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        url_to_entry: dict[str, dict],
        job_id: str,
        batch_tag: str | None = None,
        content_config: dict | None = None,
    ) -> None:
        """Consume batch results via cursor pagination, processing each page inline.

        Tracks our own skip offset to avoid re-processing pages on each sleep
        cycle. After the batch finishes, any URLs not returned by Firecrawl
        (batch-side failures) are individually retried.

        changeTracking data embedded in each batch result page is forwarded to
        process_article_result so unchanged pages are skipped without a DB read.
        """
        base_url = f"{self.base_url}/v2/batch/scrape/{job_id}"
        skip = 0
        processed_urls: set[str] = set()

        while True:
            poll_url = f"{base_url}?skip={skip}" if skip > 0 else base_url
            data = await self._get_batch_status(poll_url)
            status = data.get("status", "")
            completed = data.get("completed", 0)
            total = data.get("total", 0)

            pages = data.get("data", [])
            for page in pages:
                meta = page.get("metadata", {})
                url = meta.get("sourceURL") or meta.get("url", "")
                markdown = page.get("markdown", "")
                html = page.get("html", "")

                # Extract changeTracking data from batch result
                ct = page.get("changeTracking") or {}
                change_status = ct.get("changeStatus")
                diff_text = (ct.get("diff") or {}).get("text")

                entry = url_to_entry.get(url)
                if not entry:
                    logger.warning("Batch result URL not in TOC: %s", url)
                    processed_urls.add(url)
                    continue

                # Retry empty-content responses individually (preserves changeTracking)
                if not markdown.strip():
                    logger.warning(
                        "Empty content for %s from batch — retrying individually", url
                    )
                    for attempt in range(self.EMPTY_CONTENT_RETRIES):
                        await asyncio.sleep(self.EMPTY_CONTENT_RETRY_DELAY)
                        markdown, html, change_status, diff_text = await self._scrape_article(
                            url, tag=batch_tag, content_config=content_config
                        )
                        if markdown.strip():
                            break
                        logger.warning(
                            "Still empty for %s (retry %d/%d)",
                            url, attempt + 1, self.EMPTY_CONTENT_RETRIES,
                        )

                if change_status:
                    logger.info(
                        "[%d/%d] %s (%s): %s",
                        completed, total, url, change_status, entry["title"],
                    )
                else:
                    logger.info("[%d/%d] Processing: %s", completed, total, url)
                await self.process_article_result(
                    db=db,
                    source_id=source_id,
                    run_id=run_id,
                    url=url,
                    markdown_content=markdown,
                    doc_html=html,
                    toc_entry_id=entry.get("toc_entry_id"),
                    sort_order=entry.get("sort_order", 0),
                    title=entry["title"],
                    change_status=change_status,
                    diff_text=diff_text,
                )
                processed_urls.add(url)

            skip += len(pages)

            if pages:
                continue  # immediately poll for more

            if status == "completed":
                logger.info(
                    "Batch %s complete: %d/%d", job_id, completed, total
                )
                break

            # No new results yet and job still running — wait
            await asyncio.sleep(self.BATCH_POLL_INTERVAL)

        # Retry any URLs Firecrawl silently dropped (batch-side failures)
        missing = [url for url in url_to_entry if url not in processed_urls]
        if missing:
            logger.warning(
                "Batch %s dropped %d URLs — retrying individually: %s…",
                job_id, len(missing), missing[:3],
            )
            for url in missing:
                entry = url_to_entry[url]
                logger.info("Individual retry: %s", url)
                try:
                    markdown, html, change_status, diff_text = await self._scrape_article_with_retry(
                        url, tag=batch_tag, content_config=content_config
                    )
                    await self.process_article_result(
                        db=db,
                        source_id=source_id,
                        run_id=run_id,
                        url=url,
                        markdown_content=markdown,
                        doc_html=html,
                        toc_entry_id=entry.get("toc_entry_id"),
                        sort_order=entry.get("sort_order", 0),
                        title=entry["title"],
                        change_status=change_status,
                        diff_text=diff_text,
                    )
                except Exception as exc:
                    logger.warning("Individual retry failed for %s: %s", url, exc)

    async def _reconcile_removals(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID
    ) -> None:
        """Stamp pages that dropped out of the rebuilt TOC, clear ones that returned.

        Runs after all pages are processed (and re-linked), so the set of articles
        with toc_entry_id IS NULL is exactly the removed pages. removed_at is only
        set when currently NULL, so it stays pinned to first detection across runs.
        """
        now = datetime.now(timezone.utc)
        # Newly removed.
        await db.execute(
            update(Article)
            .where(
                Article.source_id == source_id,
                Article.toc_entry_id.is_(None),
                Article.removed_at.is_(None),
            )
            .values(removed_at=now, removal_run_id=run_id)
        )
        # Re-added → clear the removal flag.
        await db.execute(
            update(Article)
            .where(
                Article.source_id == source_id,
                Article.toc_entry_id.isnot(None),
                Article.removed_at.isnot(None),
            )
            .values(removed_at=None, removal_run_id=None)
        )
        await db.commit()

    async def extract_source(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
    ) -> ExtractionRun:
        """Execute a full extraction for a documentation source.

        Phase 1 — TOC discovery: recursively scrapes parent nav items in DOM
        order to build a complete depth-first ordered TOC.

        Phase 2 — Content scraping: submits all TOC URLs as a single Firecrawl
        batch job. If DOCEXTRACTOR_WEBHOOK_BASE_URL is configured, Firecrawl
        pushes per-page results to our webhook endpoint and the background task
        only polls for completion. Otherwise results are consumed via cursor
        pagination inline.
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

        run.current_phase = "toc_discovery"
        source.status = SourceStatus.EXTRACTING
        await db.commit()

        try:
            await self._check_available()

            # ── Phase 1: Build ordered TOC via the source's extraction profile ──
            profile = await self._resolve_profile(source)
            await db.commit()  # persist auto-detected platform name, if any
            content_cfg = profile.content_config()
            self._content_config_by_source[source_id] = content_cfg
            logger.info(
                "Discovering TOC for %s (profile=%s)", source.base_url, profile.name
            )
            toc_objs = await profile.build_toc(source.base_url, Scraper(self))
            toc_entries = [
                {"title": e.title, "url": e.url, "level": e.level, "is_article": e.is_article}
                for e in toc_objs
            ]

            if not toc_entries:
                toc_entries = [{
                    "title": "Index",
                    "url": source.base_url,
                    "level": 0,
                    "is_article": True,
                }]

            # Deduplicate while preserving DFS order; url-less section headers
            # are always kept (never collapsed).
            toc_entries = _dedupe_toc_entries(toc_entries)

            logger.info("TOC contains %d pages", len(toc_entries))
            run.articles_total = len(toc_entries)

            # ── Persist TOC entries ─────────────────────────────────────────
            await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source_id))
            await db.flush()

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

                if td.get("url"):
                    toc_db_map[td["url"]] = toc_entry.id
                level_to_parent[td["level"]] = toc_entry.id
                for deeper in [k for k in level_to_parent if k > td["level"]]:
                    del level_to_parent[deeper]

            # Enrich entries with their persisted TOCEntry IDs for use in Phase 2
            for entry in toc_entries:
                entry["toc_entry_id"] = toc_db_map.get(entry["url"])

            # Commit Phase 1: TOC and total count visible to status poller.
            run.current_phase = "content_scraping"
            await db.commit()

            # ── Phase 2: Batch scrape all content pages ─────────────────────
            # Submit all TOC URLs in one batch job so Firecrawl can process
            # them concurrently, then consume results via cursor pagination as
            # they complete. This is strictly faster than the old sequential loop
            # and gives the UI live per-page progress via the counters.
            url_to_entry = {e["url"]: e for e in toc_entries if e.get("url")}
            batch_tag = f"src-{source_id}" if self.api_key else None
            job_id = await self._submit_batch(
                list(url_to_entry.keys()), source_id, content_config=content_cfg
            )
            run.firecrawl_job_id = job_id
            await db.commit()

            await self._poll_batch_and_process(
                db, source_id, run.id, url_to_entry, job_id, batch_tag=batch_tag,
                content_config=content_cfg,
            )

            # Record removals (pages gone from the rebuilt TOC) before completing.
            await self._reconcile_removals(db, source_id, run.id)

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
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
