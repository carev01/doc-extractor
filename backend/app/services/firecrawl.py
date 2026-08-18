"""Firecrawl integration service — full-site extraction with TOC preservation."""

import asyncio
import hashlib
import json
import base64
import logging
import os
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article import Article
from app.models.article_version import ArticleVersion
from app.models.content_change import ContentChange
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.image import ArticleImage
from app.models.product import Product
from app.models.source import DocumentationSource, SourceStatus
from app.models.toc import TOCEntry
from app.models.auth_realm import AuthRealm, RealmStatus
from app.schemas.delta import encode_delta_cursor
from app.services.auth import realm_manager
from app.services.auth.realm_manager import NeedsLoginError
from app.services.auth.session import session_expired
from app.services.blockpage import is_auth_wall, is_block_page
from app.services.notify import notify
from app.services import change_log
from app.services import image_describe
from app.services import webhook_dispatcher
from app.services.profiles import registry as profile_registry
from app.services.profiles.base import TocEntry as ProfileTocEntry
from app.services.profiles.content_scope import scope_content_html, strip_selectors
from app.services.profiles.detector import detect_platform
import app.services.profiles.llm as llm_mod
from app.services.profiles.scraper import Scraper
from app.services.sanitize import sanitize_markdown
from app.services.toc_checkpoint import TocBuildCheckpoint
from app.services.versioning import (
    VERSION_PLACEHOLDER,
    derive_topic_key,
    detect_version_token,
)
from app.core.database import async_session

# Default content scrape options when no profile config is supplied (legacy #doc).
_LEGACY_CONTENT = {"includeTags": ["#doc"], "onlyMainContent": False, "waitFor": 1500}

# Recorded on a run's error_message when a scraped page is a bot-protection /
# WAF block page (so it isn't stored). If nothing else is persisted, the run is
# failed with this message rather than reported as an empty success.
_BLOCKED_MSG = (
    "Bot protection (e.g. Akamai/Cloudflare) blocked one or more pages; those "
    "pages were not stored. The site likely needs a warm-up/stealth profile."
)
# Cap on how many blocked-page descriptors we accumulate on a run's
# blocked_pending. A partial block (the retryable case) is by definition a small
# fraction; a fully-blocked run fails loudly and doesn't need the whole list, so
# this bounds JSONB growth (and per-page UPDATE churn) on the pathological case.
_BLOCKED_PENDING_CAP = 1000

# Browser User-Agent so bot-gated sites (e.g. Confluence Cloud) render real
# content instead of a JS "unsupported browser" shell.
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Decorative theme/skin images that are not documentation content — e.g. MadCap
# Flare skin assets, system images (the copy/feedback/chat icons), and spacer
# GIFs. They repeat on every page, so downloading them per-article is pure
# overhead. Matched by URL path; conservative, so non-Flare sites are unaffected.
_BOILERPLATE_IMG_RE = re.compile(
    r"/_SystemImages/|/Skins/|/ui-icons/|/transparent\.gif$", re.IGNORECASE
)
# A base64-encoded inline image (data URI). Extracted to a stored file so the raw
# base64 never bloats the served/exported markdown.
_DATA_URI_IMG_RE = re.compile(r"data:image/([A-Za-z0-9.+-]+);base64,(.+)", re.DOTALL)

# A query string tacked onto an image-asset URL (…/foo.png?sv=…&sig=…). Many CDNs
# serve images via short-lived signed URLs (e.g. Azure Blob SAS on Document360,
# where `st`/`se`/`sig` are re-minted every scrape). Those tokens are volatile but
# reference the *same* image, so they must be neutralised before hashing — otherwise
# every scrape yields a different content_hash and the page is perpetually flagged
# "changed". Anchored on the file extension so a path containing parens
# (…/1(2).png?…) is handled correctly; the query runs to the closing markdown ")".
_VOLATILE_IMG_QUERY_RE = re.compile(
    r"(\.(?:png|jpe?g|gif|svg|webp|bmp|ico|tiff?|avif))\?[^\s)\"']*",
    re.IGNORECASE,
)


def _normalize_for_change_hash(content: str) -> str:
    """Neutralise volatile-but-meaningless bits before change-detection hashing.

    Currently strips signed/cache-busting query strings from image-asset URLs so a
    re-minted CDN token doesn't masquerade as a content change. Fingerprint-only —
    the stored/served markdown keeps its original URLs (and is later rewritten to
    stable /media paths regardless)."""
    return _VOLATILE_IMG_QUERY_RE.sub(r"\1", content)


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


def _content_selector_for(entry: dict, content_spec: dict | None) -> str | None:
    """Pick the content selector for a single TOC entry on the browserless path.

    An entry may carry its own ``content_selector`` (so two articles can be
    sliced from different sections of one page); otherwise fall back to the
    run-wide selector from the profile's ``browserless_content_spec``.
    """
    return (entry or {}).get("content_selector") or (content_spec or {}).get("selector")


class FirecrawlUnavailableError(Exception):
    """Raised when the Firecrawl service is not reachable."""
    pass


class RunControlSignal(Exception):
    """Raised inside extract_source when a cooperative control signal is seen.

    ``action`` is "cancel" or "pause". Caught by extract_source to stop cleanly
    (cancel discards the resume checkpoint; pause keeps it) — not a failure.
    """

    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action


class RawContentScrapeError(Exception):
    """Raised when the raw_http content path fails too large a fraction of pages.

    Signals a likely structural change or new bot-gating on the source, so the
    run fails loudly (via extract_source's generic handler) instead of reporting
    COMPLETED with a silently-partial doc set. The resume checkpoint is kept, so
    a re-trigger retries only the pages that failed.
    """


# Every TOC-collapse failure message starts with this, so the API can flag the
# run as overridable ("Extract anyway") without a second failure-kind column.
TOC_COLLAPSE_PREFIX = "TOC discovery collapsed"


class TocCollapseError(Exception):
    """Raised when a run's rebuilt TOC is drastically smaller than the source's
    prior live-article count — TOC discovery almost certainly failed (overloaded
    Firecrawl/Browserless, empty nav, upstream change). Raised BEFORE the
    destructive TOC rebuild so extract_source's generic handler fails the run
    without wiping the previously-good content.

    Overridable per run: trigger with ``allow_toc_collapse=true`` when the smaller
    TOC is real (the upstream doc genuinely shrank) or the baseline is stale.
    """


def _collapse_baseline(prior_live: int, last_ok_total: int | None) -> int:
    """The page count to measure a rebuilt TOC against.

    The live-article count alone is a poor baseline: it counts every live row,
    including duplicates a *past* bug left behind. Arcserve "Agent for Linux
    Guide" held 518 live articles for 259 distinct URLs (each page stored twice
    after the pre-#189 raw_http path keyed pages by their literal-version URL), so
    a healthy 255-page TOC read as 255 < 50% of 518 and every run aborted — the
    guard blocking the very run that would have re-matched and retired the
    duplicates.

    The last successful extraction's ``articles_total`` is the honest "how many
    pages did this doc have last time discovery worked" figure and is immune to
    duplicate rows, so take whichever is *lower*: a real collapse (an empty nav
    yielding 0–1 pages) trips against either, while neither a duplicated corpus
    nor a stale run total can manufacture a false positive on its own.
    """
    if last_ok_total is None or last_ok_total <= 0:
        return prior_live
    return min(prior_live, last_ok_total)


def _toc_collapsed(scrapable_total: int, baseline: int, ratio: float, min_prior: int) -> bool:
    """True when a rebuilt TOC looks catastrophically smaller than the prior
    corpus (data-loss guard). Only engages once the source had a meaningful prior
    corpus (``baseline >= min_prior``); tiny/new sources never trip it."""
    if baseline < min_prior:
        return False
    return scrapable_total < baseline * ratio


def _cookie_header(cookies: list[dict] | None) -> str | None:
    """Build a ``Cookie`` request-header value from a realm cookie list."""
    if not cookies:
        return None
    pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
    return "; ".join(pairs) or None


def _is_auth_expiry(realm) -> bool:
    """A mass raw-HTTP failure on a source that has an auth realm is treated as a
    likely session/WAF failure → pause+EXPIRED+notify (the user re-auths and
    resumes); only unauthenticated sources fail loudly via RawContentScrapeError."""
    return realm is not None


# A run of this many consecutive page failures on an *authenticated* source means
# the session has died mid-run (gated pages redirect to the IdP and yield no
# article). Pause promptly rather than churning the remaining pages.
_RAW_HTTP_AUTH_FAIL_STREAK = 15


async def _pause_for_expiry(db, src, realm) -> None:
    """Mark the realm EXPIRED, notify, and raise RunControlSignal('pause').

    The checkpoint is kept (and only successfully-scraped pages were recorded in
    it), so a fresh cookie + Resume retries exactly the pages that were missed.
    """
    await realm_manager.invalidate(
        db, realm, RealmStatus.EXPIRED, "Session expired during extraction"
    )
    await notify(
        "Extraction paused",
        f"Realm '{realm.name}' — extraction of '{src.name}' paused: the session "
        f"appears to have expired (authenticated pages are redirecting to login). "
        f"Upload a fresh cookie and hit Resume to continue.",
        realm=realm.name,
    )
    await db.commit()
    raise RunControlSignal("pause")


def _resolve_content_engine(source, profile) -> str | None:
    """Resolve the content-scraping engine for a source.

    A per-source override in ``source.profile_config["content_engine"]`` wins —
    this is how LLM-derived sources (whose runtime profile is the generic
    ``DerivedProfile``) opt into ``raw_http`` without a code change. Otherwise
    fall back to the profile class's ``content_engine`` attribute (e.g.
    ``flare_webhelp``). Returns ``None`` for the default rendered batch path.
    """
    override = (getattr(source, "profile_config", None) or {}).get("content_engine")
    return override or getattr(profile, "content_engine", None)


def _select_content_path(
    has_auth: bool, content_engine: str | None, render_engine: str | None
) -> str:
    """Pick the content-scrape path.

    A raw_http profile always uses the raw-HTTP path (cookies are injected when
    authenticated). An authenticated non-raw_http profile, or one that requires
    Browserless rendering, uses Browserless. Everything else uses the Firecrawl
    batch path.
    """
    if content_engine == "raw_http":
        return "raw_http"
    if has_auth or render_engine == "browserless":
        return "browserless"
    return "firecrawl"


def _raw_failure_exceeded(attempted: int, failed: int) -> bool:
    """True when the raw_http run should abort: enough pages were attempted and
    the failure fraction exceeds the configured ceiling. Below
    ``raw_http_min_attempts`` we never abort (a tiny source with one dead URL
    shouldn't fail the run)."""
    return (
        attempted >= settings.raw_http_min_attempts
        and failed / attempted > settings.raw_http_max_failure_rate
    )


def persisted_count(extracted: int, updated: int, unchanged: int, resumed: int) -> int:
    """Total pages accounted for in a run: freshly processed this attempt plus
    pages carried over from a resumed checkpoint."""
    return (extracted or 0) + (updated or 0) + (unchanged or 0) + (resumed or 0)


def _should_auto_retry_blocked(n_blocked: int, total: int, max_pct: float) -> bool:
    """True when a partial bot-block is small enough (≤ ``max_pct`` of ``total``
    scrapable pages) to warrant one in-run retry pass. ``max_pct`` of 0 disables
    the automatic pass."""
    if n_blocked <= 0 or total <= 0 or max_pct <= 0:
        return False
    return (100.0 * n_blocked / total) <= max_pct


def _dedup_blocked(items: list | None) -> list[dict]:
    """Blocked-page descriptors deduped by url, preserving first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for it in items or []:
        url = (it or {}).get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(it)
    return out


class _NullCheckpoint:
    """A no-op content checkpoint for a targeted re-scrape (blocked-page retry):
    reports nothing as already done (so every requested URL is scraped) and never
    persists progress, leaving the real run checkpoint untouched."""

    async def load_content_done(self) -> set[str]:
        return set()

    async def add_content_done(self, urls: list[str]) -> None:
        return None

    async def clear(self) -> None:
        return None


def _extract_fragment(html: str, selector: str) -> str | None:
    """Return the outer HTML of the first ``selector`` match, or None."""
    from bs4 import BeautifulSoup
    el = BeautifulSoup(html or "", "html.parser").select_one(selector)
    return str(el) if el is not None else None


def _toc_superset(
    rebuilt: list[ProfileTocEntry],
    scraped_pairs: list[tuple[str, str | None]],
) -> list[ProfileTocEntry]:
    """Return rebuilt TOC entries extended with flat entries for scraped articles
    whose URL is not already in the rebuilt list.

    Guarantees every scraped article has a TOC entry so ``_reconcile_removals``
    won't mark it removed just because ``rebuild_toc`` didn't include it.
    Rebuilt entries come first (preserving hierarchy order); extras are appended.
    """
    rebuilt_urls = {e.url for e in rebuilt}
    extras = [
        ProfileTocEntry(title=title or url, url=url, level=0, is_article=True, parent_url=None)
        for url, title in scraped_pairs
        if url not in rebuilt_urls
    ]
    return rebuilt + extras


class FirecrawlService:
    """Handles documentation extraction via local Firecrawl instance."""

    CONNECT_TIMEOUT = 5.0
    EMPTY_CONTENT_RETRIES = 2
    EMPTY_CONTENT_RETRY_DELAY = 2.0
    BATCH_POLL_INTERVAL = 5.0
    # Cap URLs per Firecrawl batch; large doc sets are scraped as sequential
    # chunks so we don't overwhelm Firecrawl (503s on huge batches + retries).
    MAX_BATCH_URLS = 100
    # Firecrawl can briefly return 5xx/429 or drop the connection under load
    # (it shares the Browserless engine). These are transient — retry the POST
    # with exponential backoff rather than failing the whole run on one blip.
    TRANSIENT_STATUS = (429, 502, 503, 504)
    TRANSIENT_RETRIES = 5
    TRANSIENT_BACKOFF = 4.0  # seconds; doubles each attempt (4,8,16,32,64)

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

    async def _resolve_profile(self, source: DocumentationSource, auth_cookies=None):
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
            scraper = Scraper(self, auth_cookies=auth_cookies)
            if auth_cookies:
                # Authenticated sources may have a login-gated root (e.g. EON/Fern).
                # Fetch it with the session cookies via the raw path — Firecrawl
                # /scrape (get_html) can't inject cookies, so it would see the
                # login page and detection would miss the real platform.
                root_html = await scraper.get_raw(source.base_url)
            else:
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

    async def fetch_raw(self, url: str, cookies: list[dict] | None = None,
                        retry_statuses: "set[int] | tuple[int, ...] | None" = None) -> str:
        """Plain GET of a static asset, bypassing Firecrawl's HTML cleaning.

        Used for non-HTML resources a profile needs verbatim — e.g. MadCap Flare's
        ``Data/*.xml``/``Data/Tocs/*.js`` TOC files, which Firecrawl would strip or
        mangle. Sends a browser UA; ``cookies`` (a realm session cookie list)
        are sent as a ``Cookie`` header for authenticated raw_http sources.
        Raises on HTTP error.

        Retries transient failures (429/5xx, connect/read timeouts) with backoff:
        the raw_http content path fetches hundreds of pages, so without this a
        single momentary blip or short-lived rate-limit permanently drops a page,
        and enough of those trip the run's failure-rate guard (observed on a
        700-page MadCap source). Non-transient errors (e.g. 404) still raise at once.

        When cookies are supplied, redirects are followed manually so the Cookie
        header is re-attached on every same-host hop. httpx 0.28+ explicitly
        strips the Cookie header when following redirects automatically (it
        re-derives it from the client cookie jar, which is empty here), so an
        authenticated page that 30x-redirects would otherwise land unauthenticated.
        """
        ck = _cookie_header(cookies)

        if not ck:
            # No cookies — use automatic redirect following (unchanged behaviour).
            resp = await self._request_with_retry(
                lambda: self.client.get(url, headers={"User-Agent": _BROWSER_UA}, follow_redirects=True),
                what=f"raw GET {url}",
                retry_statuses=retry_statuses,
            )
            return resp.text

        # Cookies supplied — follow redirects manually, re-attaching the Cookie
        # header on each same-host hop.
        #
        # httpx 0.28+ raise_for_status() also raises on 3xx responses, so
        # _request_with_retry raises on a redirect rather than returning it.
        # We catch HTTPStatusError for 3xx codes to extract the redirect response
        # and continue following; real errors (4xx, 5xx) are re-raised.
        headers = {"User-Agent": _BROWSER_UA, "Cookie": ck}
        original_host = httpx.URL(url).host
        current_url = url
        resp = None

        for _ in range(10):
            hop_url = current_url  # explicit capture so the lambda closes over this value
            try:
                resp = await self._request_with_retry(
                    lambda: self.client.get(hop_url, headers=headers, follow_redirects=False),
                    what=f"raw GET {hop_url}",
                    retry_statuses=retry_statuses,
                )
                # Non-redirect (2xx) final response.
                return resp.text
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else None
                if code not in (301, 302, 303, 307, 308):
                    raise  # real error, propagate
                resp = exc.response  # extract the redirect response

            location = resp.headers.get("location")
            if not location:
                return resp.text

            next_url = urljoin(current_url, location)

            # Security: do not forward cookies to a different host. A cross-host
            # redirect from a valid authenticated session should not happen for
            # SSR docs; an expired session redirecting to a different login host
            # must not receive the session cookie.
            if httpx.URL(next_url).host != original_host:
                return resp.text

            current_url = next_url

        # Redirect cap reached — return whatever the last response held.
        return resp.text if resp is not None else ""

    async def _request_with_retry(
        self, send: Callable[[], Awaitable[httpx.Response]], what: str,
        retry_statuses=None,
    ) -> httpx.Response:
        """Run an httpx request (via the `send` callable), retrying transient
        5xx/429 and transport errors (connect/read/write — e.g. a Firecrawl pod
        restart) with exponential backoff. Non-transient errors (4xx) raise
        immediately.

        ``retry_statuses`` extends the retryable set beyond ``TRANSIENT_STATUS``
        for callers that need additional codes retried (e.g. 401 on cookie-gated
        raw_http sources where the session may need a moment to propagate).
        """
        retryable = set(self.TRANSIENT_STATUS) | set(retry_statuses or ())
        delay = self.TRANSIENT_BACKOFF
        last_exc: Exception | None = None
        for attempt in range(self.TRANSIENT_RETRIES + 1):
            try:
                resp = await send()
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else None
                if code not in retryable or attempt >= self.TRANSIENT_RETRIES:
                    raise
                last_exc = exc
            except httpx.TransportError as exc:  # connect/read/write errors
                if attempt >= self.TRANSIENT_RETRIES:
                    raise
                last_exc = exc
            logger.warning(
                "%s transient failure (%s) — retry %d/%d in %.0fs",
                what, last_exc, attempt + 1, self.TRANSIENT_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
        raise last_exc  # pragma: no cover (loop always returns or raises above)

    async def _post_with_retry(self, endpoint: str, json: dict, what: str) -> httpx.Response:
        """POST to Firecrawl, retrying transient 5xx/429 and transport errors."""
        return await self._request_with_retry(
            lambda: self.client.post(endpoint, json=json, headers=self._auth_headers()),
            what,
        )

    async def _get_with_retry(self, url: str, what: str) -> httpx.Response:
        """GET from Firecrawl, retrying transient 5xx/429 and transport errors."""
        return await self._request_with_retry(
            lambda: self.client.get(url, headers=self._auth_headers()),
            what,
        )

    async def _firecrawl_request(self, url: str, payload: dict) -> dict:
        """Make a Firecrawl v2 scrape request and return the data dict."""
        # Inject a browser UA so bot-gated sites render real content. A
        # caller-provided "headers" key overrides (merged dict: UA first,
        # caller's value wins via **payload spread).
        body = {"url": url, "headers": {"User-Agent": _BROWSER_UA}, **payload}
        resp = await self._post_with_retry(
            f"{self.base_url}/v2/scrape", body, what=f"scrape {url}"
        )
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

    def _save_data_uri_image(self, data_uri: str, article_dir: str) -> "str | None":
        """Decode a ``data:image/<fmt>;base64,<b64>`` URI and store it like a
        downloaded image (so a huge base64 blob never survives inline in the
        stored/exported markdown). Returns the local filename, or None if it isn't
        a decodable base64 image."""
        m = _DATA_URI_IMG_RE.match(data_uri)
        if not m:
            return None
        fmt = m.group(1).lower()
        try:
            raw = base64.b64decode("".join(m.group(2).split()))
        except Exception:  # noqa: BLE001 — malformed base64 → leave it alone
            return None
        if not raw:
            return None
        ext = {"jpeg": ".jpg", "jpg": ".jpg", "gif": ".gif",
               "svg+xml": ".svg", "webp": ".webp"}.get(fmt, ".png")
        # Content-address by the decoded bytes (see _download_image) so an
        # unchanged inline image keeps the same filename across scrapes.
        filename = f"{hashlib.sha256(raw).hexdigest()[:16]}{ext}"
        try:
            os.makedirs(article_dir, exist_ok=True)
            with open(os.path.join(article_dir, filename), "wb") as f:
                f.write(raw)
        except OSError:
            return None
        return filename

    async def _download_image(self, img_url: str, article_dir: str,
                              auth_cookies: list[dict] | None = None) -> str | None:
        try:
            headers = {"User-Agent": _BROWSER_UA}
            ck = _cookie_header(auth_cookies)
            if ck:
                headers["Cookie"] = ck
            resp = await self.client.get(img_url, headers=headers, follow_redirects=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "").lower()
            # Authenticated sources redirect un-cookied / gated asset requests to
            # their IdP (e.g. onepassport.rubrik.com), which answers a 200 HTML
            # login page. Only persist genuine image responses so we never save a
            # login page as a bogus image. Sending the realm cookies above means
            # genuinely-gated images download directly without the IdP bounce.
            if not content_type.startswith("image/"):
                return None
            ext = ".png"
            if "jpeg" in content_type or "jpg" in content_type:
                ext = ".jpg"
            elif "gif" in content_type:
                ext = ".gif"
            elif "svg" in content_type:
                ext = ".svg"
            elif "webp" in content_type:
                ext = ".webp"

            # Content-address the file by its bytes, not a random UUID: the same
            # image then resolves to the same filename on every scrape, so the
            # rewritten /media URL is stable across runs (no phantom content
            # change) and re-downloads overwrite in place instead of piling up
            # orphaned duplicates that grow the media volume unbounded.
            sha = hashlib.sha256(resp.content).hexdigest()
            filename = f"{sha[:16]}{ext}"
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
        topic_key: str | None = None,
        pdf_images: list | None = None,
        toc_fragment: str | None = None,
        auth_cookies: list[dict] | None = None,
        detect_blocks: bool = True,
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
        match_key = topic_key or url

        if not markdown_content.strip():
            logger.warning("Empty content from %s — skipping", url)
            return "empty"

        # Reject bot-protection / WAF challenge pages (Akamai "Access Denied",
        # Cloudflare interstitials, …). They're non-empty, so without this they'd
        # be stored as a real article — silently corrupting the source. Record the
        # condition on the run so a fully-blocked extraction fails loudly (see the
        # completion path) instead of reporting COMPLETED with junk.
        # Skipped for PDFs: the content comes from one already-downloaded file, so
        # there is no per-page WAF interception to detect — and a doc section that
        # merely documents "Access Denied" errors or names a CDN would false-flag,
        # dropping real content and warning nonsensically about "blocked pages".
        if detect_blocks and is_block_page(markdown_content):
            logger.warning(
                "Bot-protection/interstitial page from %s (len=%d) — not storing",
                url, len(markdown_content),
            )
            # Record the blocked page on the run so it can be auto-retried (small
            # fraction) or retried manually. The append is a single row-locked
            # statement — safe under the concurrent webhook path — deduped by url
            # and capped (see _BLOCKED_PENDING_CAP).
            descriptor = {
                "url": url,
                "title": title,
                "toc_entry_id": str(toc_entry_id) if toc_entry_id else None,
                "sort_order": sort_order,
                "topic_key": topic_key,
            }
            await db.execute(
                text(
                    "UPDATE extraction_runs SET error_message = :msg, "
                    "blocked_pending = CASE "
                    "  WHEN blocked_pending @> CAST(:urlkey AS jsonb) THEN blocked_pending "
                    "  WHEN jsonb_array_length(COALESCE(blocked_pending, CAST('[]' AS jsonb))) >= :cap "
                    "    THEN blocked_pending "
                    "  ELSE COALESCE(blocked_pending, CAST('[]' AS jsonb)) || CAST(:item AS jsonb) "
                    "END "
                    "WHERE id = :rid"
                ),
                {
                    "msg": _BLOCKED_MSG,
                    "urlkey": json.dumps([{"url": url}]),
                    "item": json.dumps([descriptor]),
                    "cap": _BLOCKED_PENDING_CAP,
                    "rid": run_id,
                },
            )
            await db.commit()
            return "blocked"

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
                .where(Article.source_id == source_id, Article.topic_key == match_key)
                .values(
                    source_url=url,
                    topic_key=match_key,
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

        content_hash = compute_content_hash(_normalize_for_change_hash(markdown_content))

        existing_result = await db.execute(
            select(Article).where(
                Article.source_id == source_id,
                Article.topic_key == match_key,
            )
        )
        existing_article = existing_result.scalar_one_or_none()

        # Fallback: match a live article by source_url when the topic_key doesn't.
        # A stored key can differ from the freshly-derived match_key when key
        # derivation drifted between runs (e.g. a run keyed pages by their literal
        # version because url_template wasn't set yet, vs the version-independent
        # {version} key). Without this, the page is re-created as a duplicate and
        # the whole source doubles. Matching by URL updates the existing article
        # instead and normalises its key to match_key below (self-healing). Scoped
        # to the same source and live rows; limit(1) is defensive against any
        # residual same-URL duplicates.
        if existing_article is None:
            url_matches = (
                await db.execute(
                    select(Article)
                    .where(
                        Article.source_id == source_id,
                        Article.source_url == url,
                        Article.removed_at.is_(None),
                    )
                    .limit(2)
                )
            ).scalars().all()
            # Only heal by URL when it is unambiguous. A URL shared by multiple
            # live articles (PDF outline sections that start on the same #page)
            # must NOT be matched this way, or one section would overwrite a
            # sibling; leave those to topic_key matching.
            if len(url_matches) == 1:
                existing_article = url_matches[0]

        # Second fallback: match ACROSS a version bump. When the stored key is a
        # drifted literal-version key (from a pre-fix run) AND the URL also moved
        # because the version bumped, neither the topic_key nor the exact-URL match
        # above can link new→old — so the page re-creates and the whole source
        # duplicates (the CommCell / Commvault-Cloud transition). The version-
        # independent identity is match_key with {version} treated as a wildcard: a
        # stored page at ANY version whose URL fits that shape is the same article.
        # Adopt it (and normalise its key to the templated match_key below) only
        # when the match is unambiguous. Skipped for non-versioned sources
        # (match_key has no placeholder) and once keys are templated on both sides
        # (the topic_key match above already succeeds, so we never reach here).
        if existing_article is None and VERSION_PLACEHOLDER in match_key:
            # Build a LIKE pattern from the key: escape LIKE metacharacters first
            # (doc URLs commonly contain "_"), then turn the {version} placeholder
            # into a "%" wildcard so it spans whatever version segment is stored.
            esc = match_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = esc.replace(VERSION_PLACEHOLDER, "%")
            ver_matches = (
                await db.execute(
                    select(Article)
                    .where(
                        Article.source_id == source_id,
                        Article.removed_at.is_(None),
                        Article.source_url.like(pattern, escape="\\"),
                    )
                    .limit(2)
                )
            ).scalars().all()
            if len(ver_matches) == 1:
                existing_article = ver_matches[0]

        # For "new" or None change_status fall back to hash comparison. "new" happens
        # on the first extraction after changeTracking was enabled (Firecrawl has no
        # prior snapshot for this tag yet, but our DB may already have the article).
        if change_status in (None, "new"):
            if existing_article is not None and existing_article.content_hash == content_hash:
                # Unchanged content, but scraped this run — record the scrape time
                # and re-link to the freshly-rebuilt TOC entry (the prior link was
                # NULLed when the TOC was rebuilt) so the page isn't orphaned.
                # Advance source_url to this run's URL (matching the "same" branch
                # above): _reconcile_removals re-links survivors by source_url, so a
                # stale URL — e.g. a PDF section whose #page anchor shifted, or a web
                # version bump — would otherwise mis-flag this unchanged article as
                # removed.
                existing_article.extracted_at = datetime.now(timezone.utc)
                existing_article.source_url = url
                existing_article.topic_key = match_key  # normalise a drifted key
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
                # Capture the URL this snapshot lived at BEFORE the live article's
                # source_url is advanced to this run's URL below — so a previous
                # version keeps its origin link after a relocation/version bump.
                source_url=existing_article.source_url,
            )
            db.add(version)

            article = existing_article
            article.extraction_run_id = run_id
            article.toc_entry_id = toc_entry_id
            article.title = title
            article.source_url = url
            article.topic_key = match_key
            article.content_markdown = markdown_content
            article.content_html = doc_html
            if toc_fragment is not None:
                article.toc_fragment = toc_fragment
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
                topic_key=match_key,
                content_markdown=markdown_content,
                content_html=doc_html,
                toc_fragment=toc_fragment,
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
            # Filenames referenced by this scrape; anything else in the dir is a
            # stale orphan from a previous run and is pruned below.
            kept_filenames: set[str] = set()

            # Collect the content images to fetch, skipping decorative skin/system
            # images (Flare chrome that repeats on every page) and de-duplicating
            # within the page so each distinct image is fetched once.
            to_fetch: list[tuple[int, str, str]] = []  # (sort_order, raw_src, full_src)
            seen_src: set[str] = set()
            for j, img in enumerate(img_soup.find_all("img")):
                src = img.get("src", "")
                if not src:
                    continue
                # Inline base64 image: decode + store it (rewriting the markdown to
                # a /media link) instead of leaving the raw data URI inline. Done
                # here so the huge base64 blob never reaches the stored markdown.
                if src.startswith("data:image/"):
                    if src in seen_src:
                        continue
                    seen_src.add(src)
                    fn = self._save_data_uri_image(src, article_img_dir)
                    if fn:
                        kept_filenames.add(fn)
                        served_url = f"{settings.media_url_prefix}/{article.id}/{fn}"
                        db.add(ArticleImage(
                            article_id=article.id, original_url="data:image",
                            local_filename=fn, local_path=served_url,
                            alt_text=img.get("alt", ""), sort_order=j,
                        ))
                        markdown_content = markdown_content.replace(src, served_url)
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
                *(self._download_image(full_src, article_img_dir, auth_cookies=auth_cookies)
                  for (_, _, _, full_src) in to_fetch)
            )

            for (j, src, alt, full_src), local_filename in zip(to_fetch, filenames):
                if local_filename:
                    kept_filenames.add(local_filename)
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

            # Prune orphaned files left by previous scrapes (removed images, or
            # duplicates from the pre-content-addressing random-UUID naming) so the
            # media volume doesn't grow without bound on re-extraction. Guard against
            # a transient wipe: if images WERE present on the page but every download
            # failed this run (e.g. an expired SAS token mid-scrape), keep the prior
            # files rather than nuking the whole article's images — a later run heals.
            attempted = len(to_fetch) + sum(1 for s in seen_src if s.startswith("data:image/"))
            if os.path.isdir(article_img_dir) and (kept_filenames or attempted == 0):
                for stale in os.listdir(article_img_dir):
                    if stale not in kept_filenames:
                        try:
                            os.remove(os.path.join(article_img_dir, stale))
                        except OSError:
                            pass

        elif pdf_images is not None:
            # PDF source images: content-addressed bytes produced by pdf_convert
            # (segment images). Clear the article's media dir so only current figures
            # remain —
            # reaching here with pdf_images == [] (a section that lost all its
            # figures on update) still wipes the stale files. Then write each image,
            # record an ArticleImage row, and rewrite the bare canonical "<sha>.png"
            # reference to the served /media URL. The hash was already taken on the
            # canonical markdown, so served paths don't diff.
            article_img_dir = os.path.join(media_root, str(article.id))
            shutil.rmtree(article_img_dir, ignore_errors=True)
            if pdf_images:
                os.makedirs(article_img_dir, exist_ok=True)
            for i, img in enumerate(pdf_images):
                with open(os.path.join(article_img_dir, img.filename), "wb") as fh:
                    fh.write(img.data)
                served_url = f"{settings.media_url_prefix}/{article.id}/{img.filename}"
                db.add(ArticleImage(
                    article_id=article.id,
                    original_url=f"pdf:{img.filename}",
                    local_filename=img.filename,
                    local_path=served_url,
                    alt_text=img.alt or None,
                    file_size_bytes=len(img.data),
                    sort_order=i,
                ))
                markdown_content = markdown_content.replace(
                    f"]({img.filename})", f"]({served_url})"
                )

        article.content_markdown = markdown_content

        # Outbox: record the change in the same transaction as the mutation.
        await change_log.record_change(
            db,
            article=article,
            change_type="added" if outcome == "new" else "updated",
            run_id=run_id,
        )

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

        # Fire per-page webhook events (best-effort, tracked fire-and-forget).
        # Gated on the per-run plan so we do zero webhook work — no task, no DB
        # session — when nothing is subscribed to this event for this source.
        page_event = "new_page" if outcome == "new" else "updated_page"
        if webhook_dispatcher.run_has_subscribers(run_id, page_event):
            webhook_dispatcher.spawn_event(
                event_type=page_event,
                run_id=run_id,
                source_id=source_id,
                extra={
                    "page_url": url,
                    "page_title": title,
                    "article_id": str(article.id),
                },
            )

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
        resp = await self._post_with_retry(
            f"{self.base_url}/v2/batch/scrape", payload, what="batch submit"
        )
        job_id = resp.json()["id"]
        logger.info("Batch job submitted: %s (%d URLs)", job_id, len(urls))
        return job_id

    async def _get_batch_status(self, url: str) -> dict:
        """GET a batch status page (accepts both full URL and bare job ID)."""
        if not url.startswith("http"):
            url = f"{self.base_url}/v2/batch/scrape/{url}"
        resp = await self._get_with_retry(url, what=f"batch status {url}")
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

    async def _raise_if_controlled(self, db: AsyncSession, run_id: uuid.UUID) -> None:
        """Cooperative cancel/pause: raise RunControlSignal if a control signal is
        set on the run. Called at each content chunk boundary so a long scrape
        honours a cancel/pause promptly instead of running to completion."""
        ctrl = (
            await db.execute(
                select(ExtractionRun.control).where(ExtractionRun.id == run_id)
            )
        ).scalar_one_or_none()
        if ctrl:
            raise RunControlSignal(ctrl)

    CONTROL_POLL_INTERVAL = 5.0  # seconds between control checks during a long await

    async def _await_watching_control(
        self, run_id: uuid.UUID, coro: Awaitable, poll_interval: float | None = None
    ):
        """Await ``coro`` while polling for a cancel/pause signal, aborting it if one
        arrives.

        The chunk-boundary ``_raise_if_controlled`` checks only cover the content
        phase. TOC discovery is a *single* long await (a full sidebar expansion can
        run for minutes), so a cancel issued during discovery would otherwise not be
        observed until that call returned — the run looks unkillable, and the API's
        cooperative cancel silently does nothing. Racing the work against a poller
        makes discovery cancellable too.

        Polls on its own short-lived session so it never touches the caller's
        transaction concurrently (asyncpg forbids concurrent ops on one connection).
        """
        interval = poll_interval or self.CONTROL_POLL_INTERVAL
        task = asyncio.ensure_future(coro)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return task.result()
                async with async_session() as probe:
                    ctrl = (
                        await probe.execute(
                            select(ExtractionRun.control).where(ExtractionRun.id == run_id)
                        )
                    ).scalar_one_or_none()
                if ctrl:
                    raise RunControlSignal(ctrl)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _scrape_via_browserless(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        url_to_entry: dict[str, dict],
        content_spec: dict | None = None,
        auth_state: dict | None = None,
    ) -> None:
        """Content scrape for Browserless-rendered platforms: render each article
        in Browserless (real Chrome) and persist the extracted body.

        Two render modes:
        * Default (e.g. Salesforce shadow DOM): ``browserless_client.render`` walks
          light + shadow DOM for ``{contentHtml, contentText, title}``.
        * ``content_spec`` given (e.g. a support site behind Akamai): ``warmup_render`` does a
          warm-up navigation then takes the innerHTML of ``content_spec['selector']``.

        Articles are rendered in bounded-concurrency chunks (Browserless calls
        are network-bound and slow — a full SPA render each), but persisted
        sequentially since the DB session isn't concurrency-safe. Change
        detection falls back to content hashing (change_status=None), as there
        is no Firecrawl changeTracking baseline on this path.
        """
        from app.services.html_to_md import html_to_markdown

        from app.services.browserless import BrowserlessError, browserless_client

        items = list(url_to_entry.items())
        total = len(items)
        completed = 0
        chunk_size = max(1, settings.browserless_concurrency)
        client = httpx.AsyncClient(timeout=httpx.Timeout(160.0, connect=10.0))

        async def _render(url: str, entry: dict):
            try:
                # A per-entry selector (e.g. a changelog page split into two
                # section documents) overrides the run-wide content_spec.
                selector = _content_selector_for(entry, content_spec)
                if selector:
                    data = await browserless_client.warmup_render(
                        url, selector=selector,
                        warmup_url=(content_spec or {}).get("warmup_url"), client=client,
                        auth_state=auth_state,
                    )
                    # Normalise to the shape the persist loop expects, dropping
                    # any chrome the profile flags via excludeTags (the warmup
                    # render returns the selector's innerHTML, so this is an
                    # exclude-only pass — e.g. Red Hat PreviousNext / copy-link).
                    html = (data or {}).get("innerHtml", "")
                    html = strip_selectors(html, (content_spec or {}).get("excludeTags"))
                    return {
                        "contentHtml": html,
                        "contentText": "",
                        "title": (data or {}).get("title", ""),
                        # Full-page text, so an empty selector can be told apart
                        # from a WAF "Access Denied" shell (see the persist loop).
                        "bodyText": (data or {}).get("bodyText", ""),
                    }
                return await browserless_client.render(url, client=client, auth_state=auth_state)
            except BrowserlessError as exc:
                logger.warning("Browserless render failed for %s: %s", url, exc)
                return None

        try:
            for i in range(0, len(items), chunk_size):
                await self._raise_if_controlled(db, run_id)
                chunk = items[i:i + chunk_size]
                rendered = await asyncio.gather(*(_render(u, e) for u, e in chunk))
                for (url, entry), data in zip(chunk, rendered):
                    if not data:
                        continue
                    html = data.get("contentHtml") or ""
                    md = html_to_markdown(html) if html else (data.get("contentText") or "").strip()
                    # Auth wall detection — only for authenticated sources. The
                    # browserless path also serves non-auth platforms (e.g.
                    # Salesforce Help), whose article URLs may legitimately
                    # contain "login"; gating on auth_state avoids aborting those.
                    # Use the page's post-redirect URL (finalUrl) when available
                    # so an IdP bounce is caught even if the body lacks wall text.
                    if auth_state is not None and is_auth_wall(
                        md or html, final_url=data.get("finalUrl") or url
                    ):
                        raise NeedsLoginError(f"Auth wall detected at {url}; session may have expired")
                    if not md:
                        # An empty content selector can be a bot-protection/WAF
                        # shell (e.g. Akamai "Access Denied") whose block text is
                        # in the full page, not the article container. Feed that
                        # body text through process_article_result so is_block_page
                        # records it as *blocked* (surfacing a bot-protection
                        # warning + feeding the blocked-page retry) instead of
                        # silently skipping it as an empty page.
                        body_text = (data.get("bodyText") or "").strip()
                        if body_text and is_block_page(body_text):
                            try:
                                await self.process_article_result(
                                    db=db, source_id=source_id, run_id=run_id, url=url,
                                    topic_key=entry.get("topic_key"),
                                    markdown_content=body_text, doc_html="",
                                    toc_entry_id=entry.get("toc_entry_id"),
                                    sort_order=entry.get("sort_order", 0),
                                    title=entry["title"], change_status=None,
                                    auth_cookies=(auth_state or {}).get("cookies"),
                                )
                            except Exception as exc:
                                logger.warning("Failed to record blocked page %s: %s", url, exc)
                                await db.rollback()
                        else:
                            logger.warning("Empty content from %s — skipping", url)
                        continue
                    try:
                        await self.process_article_result(
                            db=db, source_id=source_id, run_id=run_id, url=url,
                            topic_key=entry.get("topic_key"),
                            markdown_content=md, doc_html=html,
                            toc_entry_id=entry.get("toc_entry_id"),
                            sort_order=entry.get("sort_order", 0),
                            title=entry["title"], change_status=None,
                            auth_cookies=(auth_state or {}).get("cookies"),
                        )
                        completed += 1
                    except Exception as exc:
                        logger.warning("Failed to process %s — skipping: %s", url, exc)
                        await db.rollback()
                logger.info("Browserless content: %d/%d processed", completed, total)
        finally:
            await client.aclose()

    async def _scrape_via_raw_http(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        run_id: uuid.UUID,
        url_to_entry: dict[str, dict],
        profile,
        checkpoint,
        auth_cookies: list[dict] | None = None,
    ) -> None:
        """Content scrape for statically-served platforms (``content_engine ==
        "raw_http"``, e.g. frame-based Flare WebHelp).

        Each topic page is fully server-rendered, so we fetch its HTML verbatim
        with a plain GET (``fetch_raw`` — no JS, no Browserless) and scope the
        body. JS rendering is deliberately skipped: some sites' scripts rewrite
        the page into a dynamic shell that drops the article body, so a rendered
        scrape captures only navigation chrome.

        Body scoping uses the profile's own ``extract_content_html`` when it
        defines one (e.g. ``flare_webhelp``); otherwise the generic
        ``scope_content_html`` applies the profile's ``content_config`` include/
        exclude selectors — so most static profiles opt in with just a
        ``content_engine`` flag and no bespoke extractor.

        Pages are fetched in bounded-concurrency chunks but persisted
        sequentially (the DB session isn't concurrency-safe). Resumable via the
        content checkpoint and cooperatively cancellable, mirroring the batch
        path. Change detection falls back to content hashing (change_status=None);
        there is no Firecrawl changeTracking baseline on this path.

        If too large a fraction of the pages attempted this run fail to fetch or
        scope (``raw_http_max_failure_rate``, once ``raw_http_min_attempts`` are
        tried), the run is failed loudly (``RawContentScrapeError``) rather than
        completing with a silently-partial doc set.
        """
        from app.services.html_to_md import html_to_markdown

        # Profile-specific extractor, else the generic selector-based scoper.
        profile_extractor = getattr(profile, "extract_content_html", None)
        if callable(profile_extractor):
            extract_body = profile_extractor
        else:
            cfg = profile.content_config()
            include = cfg.get("includeTags") or []
            exclude = cfg.get("excludeTags") or []

            def extract_body(raw: str, url: str) -> str | None:
                return scope_content_html(raw, url, include, exclude)

        frag_selector = getattr(profile, "toc_fragment_selector", None)

        items = list(url_to_entry.items())
        total = len(items)
        done = await checkpoint.load_content_done()
        pending = [(u, e) for u, e in items if u not in done]
        completed = total - len(pending)
        if completed:
            logger.info(
                "Resuming raw-HTTP content scrape for source %s: %d done, %d pending",
                source_id, completed, len(pending),
            )
            await db.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == run_id)
                .values(articles_resumed=completed)
            )
            await db.commit()

        chunk_size = max(1, getattr(profile, "raw_http_concurrency", settings.raw_http_concurrency))
        request_delay = float(getattr(profile, "raw_http_request_delay", 0) or 0)
        retry_statuses = getattr(profile, "raw_http_retry_statuses", None)
        # Failure tracking is over pages *attempted this run* (not resumed ones),
        # so the guard reflects the source's current behaviour.
        attempted = 0
        failed = 0
        # Prompt auth-expiry detection (see _pause_for_expiry): a streak of
        # failures on an authenticated source means the session died — pause now
        # instead of churning the rest, and never checkpoint a failed page.
        src = await db.get(DocumentationSource, source_id)
        realm = (
            await db.get(AuthRealm, src.auth_realm_id)
            if src is not None and src.auth_realm_id is not None
            else None
        )
        consecutive_failures = 0

        async def _fetch(url: str) -> str | None:
            try:
                if request_delay:
                    await asyncio.sleep(request_delay)
                return await self.fetch_raw(url, cookies=auth_cookies, retry_statuses=retry_statuses)
            except Exception as exc:  # network / HTTP error — skip this page
                logger.warning("Raw fetch failed for %s: %s", url, exc)
                return None

        for i in range(0, len(pending), chunk_size):
            # Cooperative cancel/pause at each chunk boundary (fresh read).
            await self._raise_if_controlled(db, run_id)

            chunk = pending[i:i + chunk_size]
            # An entry may point its body fetch at a different URL than the
            # human-facing page URL (e.g. an API endpoint returning the article
            # as JSON — see the zoomin profile); default to the page URL.
            htmls = await asyncio.gather(
                *(_fetch(e.get("content_url") or u) for u, e in chunk)
            )
            done_urls: list[str] = []
            for (url, entry), raw in zip(chunk, htmls):
                attempted += 1
                ok = False
                if not raw:
                    failed += 1
                else:
                    toc_fragment = _extract_fragment(raw, frag_selector) if frag_selector else None
                    body_html = extract_body(raw, url)
                    if not body_html:
                        logger.warning("No content body found at %s — skipping", url)
                        failed += 1
                    else:
                        md = html_to_markdown(body_html)
                        if not md:
                            logger.warning("Empty content from %s — skipping", url)
                            failed += 1
                        else:
                            try:
                                outcome = await self.process_article_result(
                                    db=db, source_id=source_id, run_id=run_id, url=url,
                                    topic_key=entry.get("topic_key"),
                                    markdown_content=md, doc_html=body_html,
                                    toc_entry_id=entry.get("toc_entry_id"),
                                    sort_order=entry.get("sort_order", 0),
                                    title=entry["title"], change_status=None,
                                    toc_fragment=toc_fragment,
                                    auth_cookies=auth_cookies,
                                )
                                if outcome in ("empty", "blocked"):
                                    failed += 1
                                else:
                                    completed += 1
                                    ok = True
                            except Exception as exc:
                                logger.warning("Failed to process %s — skipping: %s", url, exc)
                                await db.rollback()
                                failed += 1
                if ok:
                    consecutive_failures = 0
                    done_urls.append(url)
                else:
                    consecutive_failures += 1
            # Checkpoint only pages that actually succeeded, so a resume retries
            # the failed ones (e.g. login-redirect pages after a session expiry)
            # instead of skipping them forever.
            if done_urls:
                await checkpoint.add_content_done(done_urls)
            logger.info("Raw-HTTP content: %d/%d processed", completed, total)

            # Authenticated source failing many pages in a row → session died.
            # Pause promptly for re-auth rather than churning the remaining pages.
            if realm is not None and consecutive_failures >= _RAW_HTTP_AUTH_FAIL_STREAK:
                logger.warning(
                    "raw_http: %d consecutive failures on authenticated source %s "
                    "— pausing for re-auth", consecutive_failures, source_id,
                )
                await _pause_for_expiry(db, src, realm)

        # Abort a run that mostly failed — a structural change or new bot-gating,
        # not a healthy partial. The checkpoint is kept so a re-trigger retries
        # only the failed pages.
        # Fallback guard: a run that mostly failed but didn't trip the streak
        # check above — for an authenticated source treat it as an expired
        # session (pause + notify), otherwise fail loudly.
        if _raw_failure_exceeded(attempted, failed):
            if _is_auth_expiry(realm):
                await _pause_for_expiry(db, src, realm)
            raise RawContentScrapeError(
                f"raw_http content scrape failed {failed}/{attempted} pages "
                f"(> {settings.raw_http_max_failure_rate:.0%}) for source "
                f"{source_id} — likely a structural change or new bot-gating."
            )

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
                        topic_key=entry.get("topic_key"),
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
                        topic_key=entry.get("topic_key"),
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

    async def _persist_toc(
        self,
        db: AsyncSession,
        source_id: uuid.UUID,
        toc_entries: list[dict],
    ) -> dict[str, uuid.UUID]:
        """Delete existing TOCEntry rows for *source_id*, insert *toc_entries*, and
        return a ``{url: toc_entry_id}`` map for every entry that has a URL.

        ``toc_entries`` dicts must carry: ``title, url, level, is_article,
        parent_url, sort_order``.  Extra keys (``topic_key``, ``content_selector``,
        etc.) are silently ignored.

        Used by both the phase-1 TOC persistence and the post-scrape rebuild branch
        so the delete/insert/parent-link logic lives in exactly one place.
        """
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

        return toc_db_map

    async def _reconcile_removals(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID
    ) -> None:
        """Stamp pages that dropped out of the rebuilt TOC, clear ones that returned.

        Phase 1 deletes the old TOC, which NULLs every article's toc_entry_id via
        the ON DELETE SET NULL FK; Phase 2 re-links each page as it is scraped.
        But a *resumed* run skips pages already scraped in a prior cycle, so those
        articles would never be re-linked and would be wrongly flagged removed.
        Guard against that by authoritatively re-linking articles still NULL after
        process_article_result (pages a resumed run skipped) to their current TOC
        entry by URL — so the set of articles still NULL after this is exactly the
        pages genuinely absent from the rebuilt TOC.

        removed_at is only set when currently NULL, so it stays pinned to first
        detection across runs.

        Version-bump invariant: this matches on ``source_url`` (not the
        version-independent ``topic_key``) on purpose. ``process_article_result``
        runs first and advances a surviving article's ``source_url`` to the new
        version's URL (on both the changed and ``change_status == "same"`` paths),
        so by the time we get here every survivor's ``source_url`` again equals
        its new TOC entry's URL. Only genuinely-dropped topics keep their old URL
        and stay NULL. If a future change stops advancing ``source_url`` here,
        switch this re-link to ``topic_key`` or bumps will mis-flag survivors.
        """
        # Re-link by URL: each article points at the current TOC entry sharing its
        # source_url, or stays NULL if its page is truly gone from the TOC. When
        # several TOC entries share a URL — PDF outline sections that start on the
        # same ``#page`` anchor — prefer the entry whose title matches this
        # article's, so a section relinks to its own entry instead of an arbitrary
        # sibling. For web sources (one TOC entry per URL) there is a single
        # candidate, so the title ordering is a no-op.
        relink = (
            select(TOCEntry.id)
            .where(
                TOCEntry.source_id == source_id,
                TOCEntry.url == Article.source_url,
            )
            .order_by((TOCEntry.title == Article.title).desc())
            .limit(1)
            .scalar_subquery()
        )
        await db.execute(
            update(Article)
            .where(
                Article.source_id == source_id,
                Article.toc_entry_id.is_(None),
            )
            .values(toc_entry_id=relink)
        )

        now = datetime.now(timezone.utc)
        # Always capture the newly-removed rows: needed for the outbox, and reused
        # for the removed_page webhook payloads when a subscriber exists.
        newly_removed = (
            await db.execute(
                select(
                    Article.id, Article.title, Article.source_url, Article.topic_key
                ).where(
                    Article.source_id == source_id,
                    Article.toc_entry_id.is_(None),
                    Article.removed_at.is_(None),
                )
            )
        ).all()
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

        # Outbox: one removed row per newly-removed article, same transaction.
        if newly_removed:
            await change_log.record_removals(
                db, rows=newly_removed, source_id=source_id, run_id=run_id
            )
        await db.commit()

        # Fire removed_page webhook events (best-effort, tracked fire-and-forget).
        if webhook_dispatcher.run_has_subscribers(run_id, "removed_page"):
            for row in newly_removed:
                webhook_dispatcher.spawn_event(
                    event_type="removed_page",
                    run_id=run_id,
                    source_id=source_id,
                    extra={
                        "page_url": row.source_url,
                        "page_title": row.title,
                        "article_id": str(row.id),
                    },
                )

    async def retry_escalation_run(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID,
    ) -> ExtractionRun:
        """Worker entrypoint for a kind="escalate" run: re-attempt the VLM
        escalation that failed on a prior run, re-converting only the recorded
        page ranges (no Layer-A re-conversion). Mirrors extract_source's
        source-load + auth-resolution, then delegates to pdf_import."""
        source = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )).scalar_one_or_none()
        if not source:
            raise ValueError(f"Source {source_id} not found")
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one_or_none()
        if run is None:
            raise ValueError(f"ExtractionRun {run_id} not found")
        run.status = RunStatus.RUNNING
        await db.commit()
        run_pk = run.id

        auth_state: dict | None = None
        if source.auth_realm_id is not None:
            realm = await db.get(AuthRealm, source.auth_realm_id)
            try:
                if realm is None:
                    raise NeedsLoginError("Auth realm not found for source")
                auth_state = await realm_manager.ensure_session(db, realm)
            except NeedsLoginError as exc:
                run.status = RunStatus.FAILED
                run.error_message = f"Authenticated source needs login: {exc}"
                run.completed_at = datetime.now(timezone.utc)
                source.status = SourceStatus.FAILED
                source.error_message = str(exc)[:4096]
                await db.commit()
                return run

        from app.services import pdf_import
        try:
            return await pdf_import.retry_escalation(
                self, db, source, run, run_pk, auth_state=auth_state
            )
        except RunControlSignal:
            await db.rollback()
            run = (await db.execute(
                select(ExtractionRun).where(ExtractionRun.id == run_pk)
            )).scalar_one()
            run.status = RunStatus.CANCELLED
            run.completed_at = datetime.now(timezone.utc)
            src = await db.get(DocumentationSource, source_id)
            if src is not None:
                src.status = SourceStatus.COMPLETED
            await db.commit()
            return run

    async def enrich_source_run(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID,
    ) -> ExtractionRun:
        """Worker entrypoint for a kind='enrich' run: describe ALL of a source's
        missing images (no scrape, no per-run budget). Mirrors retry_escalation_run."""
        source = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )).scalar_one()
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one()
        run.current_phase = "image_enrich"
        source.status = SourceStatus.EXTRACTING
        await webhook_dispatcher.prepare_run(db, run_id, source_id)
        # Committed floor so the delta feed withholds this run's mid-run rows.
        await change_log.record_run_start(db, source_id=source_id, run_id=run_id)
        await db.commit()

        # Drain ALL missing images (max_new huge = unlimited).
        described = await image_describe.enrich_run_images(db, source_id, run_id, max_new=10**9)

        now = datetime.now(timezone.utc)
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one()
        # Run counters reflect ARTICLES changed (one 'updated' outbox row per enriched
        # article), consistent with the webhook's delta block. `described` is the image
        # count — logged, not stored on the article-scoped counters.
        counts = await change_log.run_change_counts(db, run_id)
        run.articles_updated = counts["updated"]
        run.articles_extracted = counts["updated"]
        run.articles_total = counts["updated"]
        run.status = RunStatus.COMPLETED
        run.completed_at = now
        source.status = SourceStatus.COMPLETED
        source.last_extracted_at = now
        await db.flush()
        logger.info(
            "enrich run %s: described %d images across %d articles",
            run_id, described, counts["updated"],
        )

        # Nudge the downstream to pull (same delta summary as extract_source).
        if webhook_dispatcher.run_has_subscribers(run_id, "extraction_complete"):
            max_seq = (await db.execute(select(func.max(ContentChange.id)))).scalar() or 0
            webhook_dispatcher.spawn_event(
                event_type="extraction_complete", run_id=run_id, source_id=source_id,
                extra={
                    "status": "completed", "articles_extracted": counts["updated"],
                    "articles_updated": counts["updated"], "articles_unchanged": 0, "articles_resumed": 0,
                    "delta": {
                        "added": counts["added"], "updated": counts["updated"],
                        "removed": counts["removed"], "watermark": encode_delta_cursor(max_seq),
                    },
                },
            )
        webhook_dispatcher.finish_run(run_id)
        return run

    async def _rescrape_blocked(
        self,
        db: AsyncSession,
        source: "DocumentationSource",
        run_pk: uuid.UUID,
        items: list[dict],
        profile,
        content_cfg: dict | None,
        path: str,
        auth_cookies: list[dict] | None,
        auth_state: dict | None,
    ) -> None:
        """Re-scrape a set of previously bot-blocked pages via the same engine the
        source uses, reusing the per-engine scrapers on just those URLs with a
        no-op checkpoint (so each is fetched fresh). ``process_article_result``
        re-appends any page that is *still* blocked to the run's blocked_pending;
        pages that come back clean are stored normally. Best-effort — the caller
        treats whatever remains in blocked_pending as the still-blocked set."""
        url_to_entry: dict[str, dict] = {}
        for it in items:
            url = it.get("url")
            if not url:
                continue
            tid = it.get("toc_entry_id")
            url_to_entry[url] = {
                "url": url,
                "title": it.get("title") or url,
                "toc_entry_id": uuid.UUID(tid) if tid else None,
                "sort_order": it.get("sort_order", 0),
                "topic_key": it.get("topic_key"),
            }
        if not url_to_entry:
            return
        ckpt = _NullCheckpoint()
        if path == "raw_http":
            await self._scrape_via_raw_http(
                db, source.id, run_pk, url_to_entry, profile, ckpt,
                auth_cookies=auth_cookies,
            )
        elif path == "browserless":
            spec_fn = getattr(profile, "browserless_content_spec", None)
            content_spec = spec_fn() if callable(spec_fn) else None
            await self._scrape_via_browserless(
                db, source.id, run_pk, url_to_entry,
                content_spec=content_spec, auth_state=auth_state,
            )
        else:
            batch_tag = f"src-{source.id}" if self.api_key else None
            urls = list(url_to_entry.keys())
            for i in range(0, len(urls), self.MAX_BATCH_URLS):
                chunk = urls[i:i + self.MAX_BATCH_URLS]
                chunk_map = {u: url_to_entry[u] for u in chunk}
                job_id = await self._submit_batch(
                    chunk, source.id, content_config=content_cfg
                )
                await self._poll_batch_and_process(
                    db, source.id, run_pk, chunk_map, job_id,
                    batch_tag=batch_tag, content_config=content_cfg,
                )

    async def retry_blocked_run(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID,
    ) -> ExtractionRun:
        """Worker entrypoint for a kind="retry_blocked" run: re-scrape only the
        pages a prior run recorded as bot-blocked (carried on this run's
        blocked_pending), with no TOC re-discovery. Mirrors retry_escalation_run:
        load source, resolve auth, then re-scrape and finalize."""
        source = (await db.execute(
            select(DocumentationSource).where(DocumentationSource.id == source_id)
        )).scalar_one()
        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_id)
        )).scalar_one()
        run.status = RunStatus.RUNNING
        run.current_phase = "retry_blocked"
        source.status = SourceStatus.EXTRACTING
        await webhook_dispatcher.prepare_run(db, run_id, source_id)
        await change_log.record_run_start(db, source_id=source_id, run_id=run_id)
        await db.commit()
        run_pk = run.id

        items = _dedup_blocked(run.blocked_pending)

        auth_state: dict | None = None
        if source.auth_realm_id is not None:
            realm = await db.get(AuthRealm, source.auth_realm_id)
            try:
                if realm is None:
                    raise NeedsLoginError("Auth realm not found for source")
                auth_state = await realm_manager.ensure_session(db, realm)
            except NeedsLoginError as exc:
                run.status = RunStatus.FAILED
                run.error_message = f"Authenticated source needs login: {exc}"
                run.completed_at = datetime.now(timezone.utc)
                source.status = SourceStatus.FAILED
                source.error_message = str(exc)[:4096]
                await db.commit()
                return run

        now = datetime.now(timezone.utc)
        if items:
            auth_cookies = (auth_state or {}).get("cookies")
            await self._check_available()
            profile = await self._resolve_profile(source, auth_cookies=auth_cookies)
            content_cfg = profile.content_config()
            self._content_config_by_source[source_id] = content_cfg
            path = _select_content_path(
                auth_state is not None,
                _resolve_content_engine(source, profile),
                getattr(profile, "render_engine", None),
            )
            # Clear the carried list; process_article_result re-appends only the
            # pages that are still blocked after this pass.
            run.blocked_pending = None
            run.error_message = None
            await db.commit()
            try:
                await self._rescrape_blocked(
                    db, source, run_pk, items, profile, content_cfg, path,
                    auth_cookies, auth_state,
                )
            except Exception:
                logger.exception("Blocked-page retry scrape failed for run %s", run_pk)
                await db.rollback()

        run = (await db.execute(
            select(ExtractionRun).where(ExtractionRun.id == run_pk)
        )).scalar_one()
        remaining = _dedup_blocked(run.blocked_pending)
        run.status = RunStatus.COMPLETED
        run.completed_at = now
        if remaining:
            run.error_message = _BLOCKED_MSG
            run.blocked_pending = remaining
        else:
            run.blocked_pending = None
            if run.error_message == _BLOCKED_MSG:
                run.error_message = None
        source.status = SourceStatus.COMPLETED
        source.last_extracted_at = now
        await db.flush()
        logger.info(
            "retry_blocked run %s: %d requested, %d still blocked",
            run_pk, len(items), len(remaining),
        )
        webhook_dispatcher.finish_run(run_id)
        return run

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
        # Capture the run PK now, while it's still safe to read. The content
        # phase commits and — on a per-page error — rolls back the session; a
        # rollback expires (and can detach) the in-memory run, after which even a
        # bare ``run.id`` read attempts lazy IO and raises MissingGreenlet on the
        # async engine. We reload ``run`` from this PK before the completion path.
        run_pk = run.id

        # Commit a run_start sentinel into the outbox before any article work, so
        # this run has a COMMITTED floor in content_changes.id space from the moment
        # it is active. The delta feed's safe-ceiling keys on active-run rows; the
        # committed floor closes the flush→commit window where a run's first real
        # row is assigned an id but not yet visible (which could otherwise let a
        # concurrent run's higher-id row be served and advance the cursor past the
        # uncommitted lower id). See services/delta_feed.py. Harmless on resume
        # (an extra, higher-id floor that doesn't lower the run's true minimum).
        await change_log.record_run_start(db, source_id=source_id, run_id=run_pk)
        await db.commit()

        # Authenticated source: resolve an auth state dict up front. If the
        # realm needs a human login or has no usable session, fail the run
        # cleanly instead of proceeding and scraping a login page.
        auth_state: dict | None = None
        if source.auth_realm_id is not None:
            realm = await db.get(AuthRealm, source.auth_realm_id)
            try:
                if realm is None:
                    raise NeedsLoginError("Auth realm not found for source")
                auth_state = await realm_manager.ensure_session(db, realm)
            except NeedsLoginError as exc:
                run.status = RunStatus.FAILED
                run.error_message = f"Authenticated source needs login: {exc}"
                run.completed_at = datetime.now(timezone.utc)
                source.status = SourceStatus.FAILED
                source.error_message = str(exc)[:4096]
                await db.commit()
                return run

        if source.source_type == "pdf":
            from app.services import pdf_import
            try:
                return await pdf_import.run_pdf_extraction(
                    self, db, source, run, run_pk, auth_state=auth_state
                )
            except RunControlSignal as sig:
                # Cooperative cancel/pause during PDF extraction — terminalize
                # cleanly (not FAILED). Reload by PK in case the session detached.
                await db.rollback()
                now = datetime.now(timezone.utc)
                run = (await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_pk)
                )).scalar_one()
                source = (await db.execute(
                    select(DocumentationSource).where(DocumentationSource.id == source_id)
                )).scalar_one()
                run.control = None
                if sig.action == "pause":
                    run.status = RunStatus.PAUSED
                    run.completed_at = None
                else:
                    run.status = RunStatus.CANCELLED
                    run.completed_at = now
                source.status = SourceStatus.PENDING
                source.error_message = None
                await db.commit()
                return run
            except Exception as exc:
                # Any PDF-pipeline failure (download error, corrupt/unparseable
                # PDF, conversion error, …) must mark the run FAILED rather than
                # leaving it orphaned in RUNNING (which the uq_active_run_per_source
                # index would then use to block re-runs). Roll back first: a failure
                # mid-transaction aborts the session and detaches the in-memory
                # run/source, so reload both by PK before writing the failure state.
                logger.exception("PDF extraction failed for source %s", source_id)
                await db.rollback()
                # A TRANSIENT acquire failure (e.g. a Browserless 502 while fetching
                # the PDF, or Dell's flaky CDN) is retryable: re-raise so the worker
                # requeues the run with backoff (worker.run_one) instead of hard-
                # failing. Committing FAILED here would swallow the error before the
                # worker's retry logic ever sees it. Nothing FAILED is written, so the
                # run stays claimable for the next attempt.
                if isinstance(exc, pdf_import.PdfAcquireError) and getattr(exc, "retryable", False):
                    raise
                run = (await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_pk)
                )).scalar_one()
                source = (await db.execute(
                    select(DocumentationSource).where(DocumentationSource.id == source_id)
                )).scalar_one()
                now = datetime.now(timezone.utc)
                run.status = RunStatus.FAILED
                run.error_message = str(exc)[:4096]
                run.completed_at = now
                source.status = SourceStatus.FAILED
                source.error_message = str(exc)[:4096]
                source.last_extracted_at = now
                await db.commit()
                return run

        try:
            await self._check_available()

            # Resolve, once per run, which webhook events have a subscriber (and
            # the source's display names) so per-page dispatch does no DB work
            # when nothing is subscribed. Best-effort; never blocks extraction.
            await webhook_dispatcher.prepare_run(db, run_pk, source_id)

            # ── Phase 1: Build ordered TOC via the source's extraction profile ──
            auth_cookies = (auth_state or {}).get("cookies")
            profile = await self._resolve_profile(source, auth_cookies=auth_cookies)
            await db.commit()  # persist auto-detected platform name, if any
            content_cfg = profile.content_config()
            self._content_config_by_source[source_id] = content_cfg

            # Load the product version so we can tag the run and derive topic_keys.
            product_version = (
                await db.execute(
                    select(Product.version).where(Product.id == source.product_id)
                )
            ).scalar_one_or_none()
            run.version = product_version

            # Auto-detect the url_template during the run when it's missing but the
            # version is known and appears in base_url. url_template is otherwise
            # only set via the sources API, so a versioned source configured
            # without it would key articles by their literal version — persist a
            # detected template so keys stay version-independent going forward.
            if product_version and not source.url_template:
                detected = detect_version_token(source.base_url, product_version)
                if detected:
                    source.url_template = detected
                    logger.info(
                        "Auto-detected url_template for %s: %s",
                        source.base_url, detected,
                    )
                    await db.commit()

            logger.info(
                "Discovering TOC for %s (profile=%s)", source.base_url, profile.name
            )
            # A persistent checkpoint lets a long sidebar expansion (e.g.
            # a ~9,670-node tree) resume after an interruption instead
            # of restarting; profiles that don't expand section-by-section ignore
            # it. Uses its own sessions so progress is independent of this run's
            # transaction.
            checkpoint = TocBuildCheckpoint(async_session, source_id)
            # Watched so a cancel/pause issued *during* discovery aborts the build
            # instead of being ignored until this single long call returns.
            toc_objs = await self._await_watching_control(
                run_pk,
                profile.build_toc(
                    source.base_url,
                    Scraper(self, checkpoint=checkpoint, auth_cookies=auth_cookies),
                ),
            )
            toc_entries = [
                {
                    "title": e.title, "url": e.url, "level": e.level,
                    "is_article": e.is_article, "parent_url": e.parent_url,
                    "content_selector": e.content_selector,
                    "content_url": e.content_url,
                    "topic_key": derive_topic_key(e.url, source.url_template, product_version),
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

            # ── TOC-collapse guard (data-loss protection) ───────────────────
            # A drastically smaller TOC than the source's prior live corpus means
            # discovery almost certainly failed (overloaded/unavailable
            # Firecrawl/Browserless, an empty nav render, an upstream change). If
            # we proceeded, Phase 1 would delete the old TOC and the completion
            # reconcile would mark every now-absent page removed — wiping good
            # content. Abort HERE, before any destructive write, so the prior TOC
            # and articles are untouched and the run fails loudly for a human.
            prior_live = (
                await db.execute(
                    select(func.count())
                    .select_from(Article)
                    .where(Article.source_id == source_id, Article.removed_at.is_(None))
                )
            ).scalar() or 0
            # Page count of the last extraction that actually completed. Only
            # kind="extract" runs discover a TOC (escalate/enrich leave
            # articles_total at 0), so restrict to those with a real total.
            last_ok_total = (
                await db.execute(
                    select(ExtractionRun.articles_total)
                    .where(
                        ExtractionRun.source_id == source_id,
                        ExtractionRun.id != run_pk,
                        ExtractionRun.status == RunStatus.COMPLETED,
                        ExtractionRun.kind == "extract",
                        ExtractionRun.articles_total > 0,
                    )
                    .order_by(ExtractionRun.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            baseline = _collapse_baseline(prior_live, last_ok_total)
            if _toc_collapsed(
                scrapable_total, baseline,
                settings.toc_collapse_min_ratio, settings.toc_collapse_min_prior,
            ):
                if run.allow_toc_collapse:
                    # Explicit per-run override: the operator has seen the numbers
                    # and accepts that pages absent from the new TOC get retired.
                    logger.warning(
                        "TOC-collapse guard OVERRIDDEN for %s: proceeding with %d "
                        "scrapable page(s) against a baseline of %d — pages missing "
                        "from the rebuilt TOC will be marked removed",
                        source.name, scrapable_total, baseline,
                    )
                else:
                    raise TocCollapseError(
                        f"{TOC_COLLAPSE_PREFIX} for '{source.name}': found "
                        f"{scrapable_total} scrapable page(s) vs a baseline of "
                        f"{baseline} (< {settings.toc_collapse_min_ratio:.0%}; "
                        f"{prior_live} live article(s), last completed run had "
                        f"{last_ok_total if last_ok_total else 'no'} page(s)). "
                        f"Aborting before any removal to avoid wiping good content — "
                        f"likely a scraper or upstream (Firecrawl/Browserless) "
                        f"failure. Re-run when healthy, or use Extract anyway to "
                        f"override if the doc set really did shrink."
                    )

            # ── Persist TOC entries ─────────────────────────────────────────
            toc_db_map = await self._persist_toc(db, source_id, toc_entries)

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

            path = _select_content_path(
                auth_state is not None,
                _resolve_content_engine(source, profile),
                getattr(profile, "render_engine", None),
            )
            if path == "raw_http":
                # Statically-served content: fetch each page verbatim (no JS) and
                # scope the body ourselves. A JS-rendering scrape would capture
                # only the dynamic shell. The engine may come from the profile
                # class (e.g. flare_webhelp) or a per-source profile_config
                # override (e.g. LLM-derived category_accordion sources).
                # Cookies are injected when the source is authenticated (raw_http
                # profiles that sit behind a cookie-gated login).
                await self._scrape_via_raw_http(
                    db, source_id, run_pk, url_to_entry, profile, checkpoint,
                    auth_cookies=auth_cookies,
                )
                # Post-scrape TOC rebuild: profiles that capture a per-page TOC
                # fragment (toc_fragment_selector) can reconstruct the full
                # authored hierarchy from those fragments, replacing the flat
                # inventory TOC produced by phase 1.
                if getattr(profile, "rebuild_toc", None):
                    rows = (await db.execute(
                        select(Article.source_url, Article.toc_fragment)
                        .where(
                            Article.source_id == source_id,
                            Article.removed_at.is_(None),
                            Article.toc_fragment.is_not(None),
                        )
                    )).all()
                    fragments = [(u, f) for (u, f) in rows if f]
                    if fragments:
                        try:
                            # Stitching ~100s of MB of fragments is CPU-heavy;
                            # run it off the event loop so it doesn't block the
                            # worker heartbeat.
                            rebuilt = await asyncio.to_thread(
                                profile.rebuild_toc, fragments, source.base_url
                            )
                            if rebuilt:
                                # Superset: add flat entries for scraped articles not
                                # covered by the rebuilt hierarchy so
                                # _reconcile_removals won't mark them removed.
                                scraped_pairs = (await db.execute(
                                    select(Article.source_url, Article.title)
                                    .where(
                                        Article.source_id == source_id,
                                        Article.removed_at.is_(None),
                                    )
                                )).all()
                                all_entries = _toc_superset(rebuilt, list(scraped_pairs))
                                toc_dicts = [
                                    {
                                        "title": e.title,
                                        "url": e.url,
                                        "level": e.level,
                                        "is_article": e.is_article,
                                        "parent_url": e.parent_url,
                                        "sort_order": i,
                                    }
                                    for i, e in enumerate(all_entries)
                                ]
                                url_to_id = await self._persist_toc(db, source_id, toc_dicts)
                                # Re-link articles to their rebuilt TOC entry in a
                                # single batched (executemany) statement rather than
                                # one awaited round-trip per URL.
                                if url_to_id:
                                    await db.execute(
                                        update(Article)
                                        .where(
                                            Article.source_id == source_id,
                                            Article.source_url == bindparam("b_url"),
                                        )
                                        .values(toc_entry_id=bindparam("b_tid")),
                                        [
                                            {"b_url": u, "b_tid": t}
                                            for u, t in url_to_id.items()
                                        ],
                                    )
                                await db.commit()
                                logger.info(
                                    "Rebuilt TOC hierarchy for %s: %d entries",
                                    source_id, len(all_entries),
                                )
                            else:
                                logger.warning(
                                    "rebuild_toc produced no entries for %s — "
                                    "keeping inventory TOC",
                                    source_id,
                                )
                        except Exception as exc:
                            logger.warning(
                                "TOC rebuild failed for %s, keeping inventory TOC: %s",
                                source_id, exc,
                            )
            elif path == "browserless":
                # Browserless-rendered platforms: Firecrawl can't get the content
                # (shadow DOM for Salesforce; Akamai block for support manuals), so render
                # each article in Browserless. A profile may supply a content_spec
                # (selector + warm-up) for the simple light-DOM extraction path.
                # Authenticated sources always use this path regardless of profile,
                # so the auth_state can be injected into every Browserless render.
                spec_fn = getattr(profile, "browserless_content_spec", None)
                content_spec = spec_fn() if callable(spec_fn) else None
                try:
                    await self._scrape_via_browserless(
                        db, source_id, run_pk, url_to_entry,
                        content_spec=content_spec, auth_state=auth_state,
                    )
                except NeedsLoginError as exc:
                    # Auth wall hit mid-run — session expired. Mark the realm
                    # EXPIRED, notify, and PAUSE (keep the checkpoint) so a fresh
                    # cookie + Resume continues from where it stopped.
                    realm = None
                    if source.auth_realm_id is not None:
                        realm = await db.get(AuthRealm, source.auth_realm_id)
                        await realm_manager.invalidate(
                            db, realm, RealmStatus.EXPIRED, str(exc)
                        )
                    await notify(
                        "Session expired",
                        f"Realm '{realm.name if realm else '?'}' expired during "
                        f"extraction of '{source.name}' — the run is PAUSED. "
                        f"Upload a fresh cookie and hit Resume to continue.",
                        realm=(realm.name if realm else None),
                    )
                    run = (
                        await db.execute(
                            select(ExtractionRun).where(ExtractionRun.id == run_pk)
                        )
                    ).scalar_one()
                    run.status = RunStatus.PAUSED
                    run.control = None
                    source.status = SourceStatus.PENDING
                    source.error_message = None
                    await db.commit()
                    return run
            else:
                # Submit in capped chunks (≤ MAX_BATCH_URLS) processed
                # sequentially, so a huge doc set doesn't overwhelm Firecrawl
                # (large single batches + empty-retry storms caused 503s).
                #
                # Resumable: URLs already scraped in this extraction cycle are
                # recorded in the checkpoint, so a failed/interrupted run that is
                # re-triggered skips them instead of re-scraping from zero.
                batch_tag = f"src-{source_id}" if self.api_key else None
                done = await checkpoint.load_content_done()
                all_urls = list(url_to_entry.keys())
                pending = [u for u in all_urls if u not in done]
                resumed = len(all_urls) - len(pending)
                if resumed:
                    logger.info(
                        "Resuming content scrape for %s: %d already done, %d pending",
                        source.base_url, resumed, len(pending),
                    )
                    # Reflect prior progress in the run's counter for an accurate bar.
                    run.articles_resumed = resumed
                    await db.commit()
                for i in range(0, len(pending), self.MAX_BATCH_URLS):
                    # Cooperative cancel/pause: the API sets run.control; we
                    # observe it (fresh read; committed by the API on another
                    # session) at each batch boundary and stop cleanly.
                    ctrl = (
                        await db.execute(
                            select(ExtractionRun.control).where(ExtractionRun.id == run_pk)
                        )
                    ).scalar_one_or_none()
                    if ctrl:
                        raise RunControlSignal(ctrl)
                    chunk = pending[i:i + self.MAX_BATCH_URLS]
                    chunk_map = {u: url_to_entry[u] for u in chunk}
                    job_id = await self._submit_batch(
                        chunk, source_id, content_config=content_cfg
                    )
                    run.firecrawl_job_id = job_id
                    await db.commit()
                    await self._poll_batch_and_process(
                        db, source_id, run_pk, chunk_map, job_id, batch_tag=batch_tag,
                        content_config=content_cfg,
                    )
                    # Checkpoint the chunk only after it's fully processed.
                    await checkpoint.add_content_done(chunk)

            # Content-phase commits/rollbacks expired (and may have detached) the
            # in-memory run; reload a live instance by its PK so the completion
            # path can read/write its attributes without triggering lazy IO.
            run = (
                await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_pk)
                )
            ).scalar_one()

            # Bot-protection auto-retry: if only a small fraction of pages were
            # blocked, do one in-line second pass over just those URLs before
            # completing — a fresh Firecrawl/Browserless session often clears a
            # transient Akamai/Cloudflare challenge. Above the threshold (or when
            # disabled via blocked_retry_max_pct=0) the pages are left for a manual
            # retry. Best-effort: a failed pass just leaves the pages blocked.
            blocked = _dedup_blocked(run.blocked_pending)
            total = run.articles_total or 0
            max_pct = settings.blocked_retry_max_pct
            if _should_auto_retry_blocked(len(blocked), total, max_pct):
                logger.info(
                    "Auto-retrying %d blocked page(s) (%.2f%% of %d ≤ %.1f%% threshold)",
                    len(blocked), 100.0 * len(blocked) / total, total, max_pct,
                )
                run.blocked_pending = None
                await db.commit()
                retry_ok = True
                try:
                    await self._rescrape_blocked(
                        db, source, run_pk, blocked, profile, content_cfg,
                        path, auth_cookies, auth_state,
                    )
                except Exception:
                    retry_ok = False
                    logger.exception("Blocked-page auto-retry failed for run %s", run_pk)
                    await db.rollback()
                run = (await db.execute(
                    select(ExtractionRun).where(ExtractionRun.id == run_pk)
                )).scalar_one()
                if not retry_ok and not run.blocked_pending:
                    # The pass errored before recording anything — keep the
                    # original record so the pages stay retryable.
                    run.blocked_pending = blocked
                    await db.commit()
            elif blocked and total > 0:
                logger.info(
                    "%d blocked page(s) = %.2f%% of %d exceeds %.1f%% threshold — "
                    "leaving for manual retry",
                    len(blocked), 100.0 * len(blocked) / total, total, max_pct,
                )

            # Record removals (pages gone from the rebuilt TOC) before completing.
            await self._reconcile_removals(db, source_id, run_pk)

            # Image enrichment phase (opt-in, best-effort): describe meaningful
            # images, inject captions, emit updated content_changes rows. Runs after
            # reconcile so removed pages are skipped; never fails the run.
            run.current_phase = "image_enrich"
            await db.commit()
            await image_describe.enrich_run_images(db, source_id, run_pk)

            # Whole run succeeded — drop the resume checkpoint (TOC + content).
            await checkpoint.clear()

            now = datetime.now(timezone.utc)

            # If bot protection blocked every page (nothing persisted), fail
            # loudly instead of reporting a misleading COMPLETED/0. Counters are
            # bumped via SQL UPDATE in process_article_result, so re-read them
            # rather than trusting the stale in-memory run.
            extracted, updated, unchanged, resumed, err, blocked_now = (
                await db.execute(
                    select(
                        ExtractionRun.articles_extracted,
                        ExtractionRun.articles_updated,
                        ExtractionRun.articles_unchanged,
                        ExtractionRun.articles_resumed,
                        ExtractionRun.error_message,
                        ExtractionRun.blocked_pending,
                    ).where(ExtractionRun.id == run_pk)
                )
            ).one()
            persisted = persisted_count(extracted, updated, unchanged, resumed)
            remaining_blocked = _dedup_blocked(blocked_now)
            if persisted == 0 and err == _BLOCKED_MSG:
                run.status = RunStatus.FAILED
                run.error_message = err
                run.blocked_pending = None  # fully blocked → nothing worth retrying
                run.completed_at = now
                source.status = SourceStatus.FAILED
                source.last_extracted_at = now
                await db.flush()
                return run

            run.status = RunStatus.COMPLETED
            run.completed_at = now
            # Blocked-page warning: keep the marker + pending list only if pages
            # remain blocked after the auto-retry; otherwise clear the transient
            # block state so a fully-recovered run reads clean.
            if remaining_blocked:
                run.error_message = _BLOCKED_MSG
                run.blocked_pending = remaining_blocked
            else:
                run.blocked_pending = None
                if err == _BLOCKED_MSG:
                    run.error_message = None
            source.status = SourceStatus.COMPLETED
            source.last_extracted_at = now

            await db.flush()

            # Fire extraction_complete webhook (best-effort, tracked fire-and-forget).
            if webhook_dispatcher.run_has_subscribers(run_pk, "extraction_complete"):
                delta_counts = await change_log.run_change_counts(db, run_pk)
                max_seq = (
                    await db.execute(select(func.max(ContentChange.id)))
                ).scalar() or 0
                webhook_dispatcher.spawn_event(
                    event_type="extraction_complete",
                    run_id=run_pk,
                    source_id=source_id,
                    extra={
                        "status": "completed",
                        "articles_extracted": int(extracted or 0),
                        "articles_updated": int(updated or 0),
                        "articles_unchanged": int(unchanged or 0),
                        "articles_resumed": int(resumed or 0),
                        "delta": {
                            "added": delta_counts["added"],
                            "updated": delta_counts["updated"],
                            "removed": delta_counts["removed"],
                            "watermark": encode_delta_cursor(max_seq),
                        },
                    },
                )
            webhook_dispatcher.finish_run(run_pk)

            return run

        except RunControlSignal as sig:
            now = datetime.now(timezone.utc)
            run.control = None  # consume the signal
            if sig.action == "pause":
                # Keep the resume checkpoint so the next claim continues.
                logger.info("Run %s paused at user request", run.id)
                run.status = RunStatus.PAUSED
                run.completed_at = None
            else:  # cancel
                logger.info("Run %s cancelled at user request", run.id)
                run.status = RunStatus.CANCELLED
                run.completed_at = now
                await checkpoint.clear()  # discard resume state
            # Either way, the source is no longer actively extracting.
            source.status = SourceStatus.PENDING
            source.error_message = None
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
