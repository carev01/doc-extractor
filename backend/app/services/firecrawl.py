"""Firecrawl integration service — full-site extraction with TOC preservation."""

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

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
import app.services.profiles.llm as llm_mod
from app.services.profiles.scraper import Scraper
from app.services.sanitize import sanitize_markdown
from app.services.toc_checkpoint import TocBuildCheckpoint
from app.core.database import async_session

# Default content scrape options when no profile config is supplied (legacy Commvault).
_LEGACY_CONTENT = {"includeTags": ["#doc"], "onlyMainContent": False, "waitFor": 1500}

# Browser User-Agent so bot-gated sites (e.g. Confluence Cloud) render real
# content instead of a JS "unsupported browser" shell.
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Decorative theme/skin images that are not documentation content — e.g. MadCap
# Flare skin assets, system images (the copy/feedback/chat icons), and spacer
# GIFs. They repeat on every page, so downloading them per-article is pure
# overhead. Matched by URL path; conservative, so non-Flare sites are unaffected.
_BOILERPLATE_IMG_RE = re.compile(
    r"/_SystemImages/|/Skins/|/transparent\.gif$", re.IGNORECASE
)


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


def _resolve_toc_parents(entries: list[dict]) -> list[int | None]:
    """Return, for each (deduped, ordered) entry, the index of its parent entry.

    Prefers the profile's explicit ``parent_url`` (resolved to the entry with
    that url) — this is robust to pages that appear at several TOC positions,
    where dedup-by-url would otherwise break a level-adjacency assumption. Falls
    back to "the most recent prior entry one level up" when no parent_url is
    given (profiles that only carry depth). Entries are in DFS pre-order, so a
    parent always precedes its children -> the returned index is always < i.
    """
    url_to_index: dict[str, int] = {}
    level_to_index: dict[int, int] = {}
    parents: list[int | None] = []
    for idx, e in enumerate(entries):
        purl = e.get("parent_url")
        parent_idx = url_to_index.get(purl) if purl else None
        if parent_idx is None and e.get("level", 0) > 0:
            parent_idx = level_to_index.get(e["level"] - 1)
        parents.append(parent_idx)

        if e.get("url"):
            url_to_index[e["url"]] = idx
        level_to_index[e["level"]] = idx
        for deeper in [lvl for lvl in level_to_index if lvl > e["level"]]:
            del level_to_index[deeper]
    return parents


class FirecrawlUnavailableError(Exception):
    """Raised when the Firecrawl service is not reachable."""
    pass


class FirecrawlService:
    """Handles documentation extraction via local Firecrawl instance."""

    CONNECT_TIMEOUT = 5.0
    EMPTY_CONTENT_RETRIES = 2
    EMPTY_CONTENT_RETRY_DELAY = 2.0
    BATCH_POLL_INTERVAL = 5.0
    # Cap URLs per Firecrawl batch; large doc sets are scraped as sequential
    # chunks so we don't overwhelm Firecrawl (503s on huge batches + retries).
    MAX_BATCH_URLS = 100

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
        1. Stored ``source.platform`` override (non-LLM) — if the name resolves
           to a registered profile that is not ``"llm"``, use it immediately.
        2. Auto-detection — scrape the root URL once and iterate registered
           profiles' ``detect()`` methods.  If a match is found, store it on
           ``source.platform`` so the caller can persist it with a DB commit.
           Skipped when ``source.platform == "llm"`` (explicit LLM override).
        3. LLM branch — entered when ``settings.llm_fallback_enabled`` is True
           OR ``source.platform == "llm"`` (explicit override, honoured even
           when the flag is off).
           - Read cached spec from ``source.profile_config["llm_spec"]``.
           - Cache miss: call ``derive_spec`` and write back to
             ``source.profile_config`` (persisted by the existing commit).
           - Return a ``DerivedProfile(spec)`` when a spec is available.
        4. Default — fall back to the generic sitemap profile.
        """
        # 1. Stored platform override — skip the LLM special-case here.
        if source.platform and source.platform != "llm":
            p = profile_registry.get(source.platform)
            if p is not None:
                return p

        # 2. Scrape root HTML (needed for both auto-detect and LLM derivation).
        root_html: str | None = None
        try:
            scraper = Scraper(self)
            root_html = await scraper.get_html(source.base_url)
        except Exception as exc:
            logger.warning(
                "Root HTML fetch failed for %s: %s", source.base_url, exc
            )

        # Auto-detect only when not explicitly set to "llm".
        if root_html is not None and source.platform != "llm":
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

        # 3. LLM branch — flag OR explicit platform=="llm".
        use_llm = settings.llm_fallback_enabled or source.platform == "llm"
        if use_llm:
            cfg = source.profile_config or {}
            spec = cfg.get("llm_spec")

            if spec:
                logger.info(
                    "LLM spec cache hit for %s — skipping re-derivation",
                    source.base_url,
                )
            else:
                html_for_llm = root_html or ""
                spec = await llm_mod.derive_spec(html_for_llm, source.base_url)
                if spec:
                    source.profile_config = {**cfg, "llm_spec": spec}
                    logger.info(
                        "LLM spec freshly derived and cached for %s",
                        source.base_url,
                    )
                else:
                    logger.warning(
                        "LLM spec derivation returned None for %s — "
                        "falling through to generic profile",
                        source.base_url,
                    )

            if spec:
                return llm_mod.DerivedProfile(spec)

        # 4. Default.
        return profile_registry.get("generic")

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

    async def map_urls(self, root_url: str) -> list[str]:
        """Return all URLs discovered under *root_url* via the Firecrawl /v2/map endpoint.

        Primary path: POST ``/v2/map`` with ``{"url": root_url}`` and return the
        ``links`` (or ``data``) list from the response.

        Fallback (any error or empty result): fetch ``<scheme>://<host>/sitemap.xml``
        directly and parse ``<loc>`` entries in document order.

        Always returns a list (never raises); on total failure returns [].
        """
        try:
            resp = await self.client.post(
                f"{self.base_url}/v2/map",
                json={"url": root_url},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            body = resp.json()
            urls: list[str] = body.get("links") or body.get("data") or []
            if urls:
                return urls
            logger.info("Firecrawl /v2/map returned empty list for %s — trying sitemap fallback", root_url)
        except Exception as exc:
            logger.warning("Firecrawl /v2/map failed for %s: %s — trying sitemap fallback", root_url, exc)

        # Sitemap fallback
        try:
            parsed = urlparse(root_url)
            sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
            resp = await self.client.get(sitemap_url, headers=self._auth_headers())
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            return [loc.get_text(strip=True) for loc in soup.find_all("loc")]
        except Exception as exc:
            logger.warning("Sitemap fallback also failed for %s: %s", root_url, exc)
            return []

    async def fetch_raw(self, url: str) -> str:
        """Plain GET of a static asset, bypassing Firecrawl's HTML cleaning.

        Used for non-HTML resources a profile needs verbatim — e.g. MadCap Flare's
        ``Data/*.xml``/``Data/Tocs/*.js`` TOC files, which Firecrawl would strip or
        mangle. Sends a browser UA; raises on HTTP error.
        """
        resp = await self.client.get(
            url, headers={"User-Agent": _BROWSER_UA}, follow_redirects=True
        )
        resp.raise_for_status()
        return resp.text

    async def _firecrawl_request(self, url: str, payload: dict) -> dict:
        """Make a Firecrawl v2 scrape request and return the data dict."""
        # Inject a browser UA so bot-gated sites render real content. A
        # caller-provided "headers" key overrides (merged dict: UA first,
        # caller's value wins via **payload spread).
        body = {"url": url, "headers": {"User-Agent": _BROWSER_UA}, **payload}
        resp = await self.client.post(
            f"{self.base_url}/v2/scrape",
            json=body,
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

        # Strip recurring site chrome (feedback widgets, back-to-top anchors,
        # copyright footers, …) before hashing/persisting so stored content is
        # clean and boilerplate churn (e.g. a yearly copyright bump) doesn't
        # register as a change. Conservative — see services/sanitize.py.
        markdown_content = sanitize_markdown(markdown_content)

        # Fast-path: Firecrawl has a prior snapshot and confirms no change.
        # Content is untouched, but we still scraped the page this run — bump
        # extracted_at so it reflects the last scrape, not the last change.
        if change_status == "same":
            # The TOC is deleted and rebuilt every run (new entry ids), so the
            # article's toc_entry_id was just NULLed by SET NULL. Re-link it (and
            # refresh the TOC-derived sort_order/title) even though the content is
            # unchanged — otherwise the page orphans and the browser hides it.
            result = await db.execute(
                update(Article)
                .where(Article.source_id == source_id, Article.source_url == url)
                .values(
                    extracted_at=datetime.now(timezone.utc),
                    toc_entry_id=toc_entry_id,
                    sort_order=sort_order,
                    title=title,
                )
            )
            if result.rowcount:
                await db.execute(
                    update(ExtractionRun)
                    .where(ExtractionRun.id == run_id)
                    .values(articles_unchanged=ExtractionRun.articles_unchanged + 1)
                )
                await db.commit()
                return "unchanged"
            # Firecrawl says "same" but we have no stored copy. This happens when an
            # earlier run seeded Firecrawl's changeTracking baseline (keyed by the
            # shared source tag) but failed before persisting the page to our DB.
            # Don't trust "same" as "already stored" — fall through and persist the
            # content scraped this run, otherwise the page is lost forever (Firecrawl
            # keeps reporting "same" on every subsequent run).
            logger.info(
                "change_status 'same' but no stored article for %s — persisting", url
            )

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

            # Collect the content images to fetch, skipping decorative skin/system
            # images (Flare chrome that repeats on every page) and de-duplicating
            # within the page so each distinct image is fetched once.
            to_fetch: list[tuple[int, str, str]] = []  # (sort_order, raw_src, full_src)
            seen_src: set[str] = set()
            for j, img in enumerate(img_soup.find_all("img")):
                src = img.get("src", "")
                if not src:
                    continue
                full_src = urljoin(url, src)
                if not full_src.startswith(("http://", "https://")):
                    continue
                if _BOILERPLATE_IMG_RE.search(full_src):
                    continue
                if full_src in seen_src:
                    continue
                seen_src.add(full_src)
                to_fetch.append((j, src, img.get("alt", ""), full_src))

            # Download all of a page's images concurrently rather than one-by-one
            # (the sequential round-trips dominated per-page processing time).
            filenames = await asyncio.gather(
                *(self._download_image(full_src, article_img_dir)
                  for (_, _, _, full_src) in to_fetch)
            )

            for (j, src, alt, full_src), local_filename in zip(to_fetch, filenames):
                if local_filename:
                    served_url = (
                        f"{settings.media_url_prefix}/{article.id}/{local_filename}"
                    )
                    db.add(ArticleImage(
                        article_id=article.id,
                        original_url=full_src,
                        local_filename=local_filename,
                        local_path=served_url,
                        alt_text=alt,
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
        content = content_config or _LEGACY_CONTENT
        # Inject a browser UA so bot-gated sites render real content. Preserve
        # any "headers" already present in content_config (caller wins).
        scrape_headers = {"User-Agent": _BROWSER_UA, **(content.get("headers") or {})}
        payload: dict = {"urls": urls, "formats": formats, **content, "headers": scrape_headers}
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

    async def _scrape_via_browserless(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        url_to_entry: dict[str, dict],
    ) -> None:
        """Content scrape for shadow-DOM platforms: render each article in
        Browserless (real Chrome) and persist the extracted body.

        Articles are rendered in bounded-concurrency chunks (Browserless calls
        are network-bound and slow — a full SPA render each), but persisted
        sequentially since the DB session isn't concurrency-safe. Change
        detection falls back to content hashing (change_status=None), as there
        is no Firecrawl changeTracking baseline on this path.
        """
        from markdownify import markdownify

        from app.services.browserless import BrowserlessError, browserless_client

        items = list(url_to_entry.items())
        total = len(items)
        completed = 0
        chunk_size = max(1, settings.browserless_concurrency)
        client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

        async def _render(url: str):
            try:
                return await browserless_client.render(url, client=client)
            except BrowserlessError as exc:
                logger.warning("Browserless render failed for %s: %s", url, exc)
                return None

        try:
            for i in range(0, len(items), chunk_size):
                chunk = items[i:i + chunk_size]
                rendered = await asyncio.gather(*(_render(u) for u, _ in chunk))
                for (url, entry), data in zip(chunk, rendered):
                    if not data:
                        continue
                    html = data.get("contentHtml") or ""
                    md = markdownify(html).strip() if html else (data.get("contentText") or "").strip()
                    if not md:
                        logger.warning("Empty content from %s — skipping", url)
                        continue
                    try:
                        await self.process_article_result(
                            db=db, source_id=source_id, run_id=run_id, url=url,
                            markdown_content=md, doc_html=html,
                            toc_entry_id=entry.get("toc_entry_id"),
                            sort_order=entry.get("sort_order", 0),
                            title=entry["title"], change_status=None,
                        )
                        completed += 1
                    except Exception as exc:
                        logger.warning("Failed to process %s — skipping: %s", url, exc)
                        await db.rollback()
                logger.info("Browserless content: %d/%d processed", completed, total)
        finally:
            await client.aclose()

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

                # Process each page defensively: a single page's failure (e.g. a
                # Firecrawl 500 on an individual retry, or a parse/DB error) must not
                # abort the whole run after other pages have succeeded. Mark it
                # processed regardless so it isn't retried into the same failure.
                try:
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
                except Exception as exc:
                    logger.warning("Failed to process %s — skipping: %s", url, exc)
                    await db.rollback()
                finally:
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
            # A persistent checkpoint lets a long sidebar expansion (e.g.
            # Commvault's ~9,670-node tree) resume after an interruption instead
            # of restarting; profiles that don't expand section-by-section ignore
            # it. Uses its own sessions so progress is independent of this run's
            # transaction.
            checkpoint = TocBuildCheckpoint(async_session, source_id)
            toc_objs = await profile.build_toc(
                source.base_url, Scraper(self, checkpoint=checkpoint)
            )
            toc_entries = [
                {
                    "title": e.title, "url": e.url, "level": e.level,
                    "is_article": e.is_article, "parent_url": e.parent_url,
                }
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

            # Count only scrapable entries (those with a URL) — structural
            # sections (e.g. MadCap placeholder "book" nodes) carry no page, so
            # they shouldn't inflate the run's article total / progress.
            scrapable_total = sum(1 for e in toc_entries if e.get("url"))
            logger.info(
                "TOC contains %d entries (%d scrapable pages)",
                len(toc_entries), scrapable_total,
            )
            run.articles_total = scrapable_total

            # ── Persist TOC entries ─────────────────────────────────────────
            await db.execute(delete(TOCEntry).where(TOCEntry.source_id == source_id))
            await db.flush()

            toc_db_map: dict[str, uuid.UUID] = {}
            parent_idxs = _resolve_toc_parents(toc_entries)
            entry_ids: list[uuid.UUID] = []

            for i, td in enumerate(toc_entries):
                pidx = parent_idxs[i]
                parent_id = entry_ids[pidx] if pidx is not None else None
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
                entry_ids.append(toc_entry.id)
                if td.get("url"):
                    toc_db_map[td["url"]] = toc_entry.id

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

            if getattr(profile, "render_engine", None) == "browserless":
                # Shadow-DOM platforms (e.g. Salesforce Help): Firecrawl can't
                # serialise the content, so render each article in Browserless.
                await self._scrape_via_browserless(db, source_id, run.id, url_to_entry)
            else:
                # Submit in capped chunks (≤ MAX_BATCH_URLS) processed
                # sequentially, so a huge doc set doesn't overwhelm Firecrawl
                # (large single batches + empty-retry storms caused 503s).
                batch_tag = f"src-{source_id}" if self.api_key else None
                all_urls = list(url_to_entry.keys())
                for i in range(0, len(all_urls), self.MAX_BATCH_URLS):
                    chunk = all_urls[i:i + self.MAX_BATCH_URLS]
                    chunk_map = {u: url_to_entry[u] for u in chunk}
                    job_id = await self._submit_batch(
                        chunk, source_id, content_config=content_cfg
                    )
                    run.firecrawl_job_id = job_id
                    await db.commit()
                    await self._poll_batch_and_process(
                        db, source_id, run.id, chunk_map, job_id, batch_tag=batch_tag,
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
