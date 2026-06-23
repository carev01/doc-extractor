"""Browserless client — a real Chrome that can run JS in the page.

Some documentation platforms (e.g. Salesforce Help, a Lightning Web Components
SPA) render their TOC and article body entirely inside **shadow DOM**. Firecrawl
serialises only the light DOM, so it returns a near-empty shell. Browserless's
``/function`` endpoint runs arbitrary JS in the rendered page, letting us pierce
shadow DOM and extract both the navigation tree and the article content.

The same Browserless instance already backs Firecrawl's engine, so the page
renders identically; we only need a different *extraction* path.
"""

import asyncio
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Browserless /function module (ESM). Navigates, waits for the SPA to hydrate,
# then walks light + shadow DOM to extract the TOC tree and the article body.
_FUNCTION_CODE = r"""
export default async function ({ page, context }) {
  const { url, waitMs } = context;
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise(r => setTimeout(r, waitMs || 9000));
  const data = await page.evaluate(() => {
    function* walk(root) {
      const els = root.querySelectorAll('*');
      for (const el of els) { yield el; if (el.shadowRoot) yield* walk(el.shadowRoot); }
    }
    // TOC: SLDS tree items, in document order, with their depth + article link.
    const toc = [];
    for (const el of walk(document)) {
      if (el.getAttribute && el.getAttribute('role') === 'treeitem') {
        let a = null;
        for (const d of walk(el)) {
          if (d.tagName === 'A') {
            const h = d.getAttribute('href') || '';
            if (h.includes('articleView') || /[?&]id=/.test(h)) { a = d; break; }
          }
        }
        if (!a) continue;
        const lvl = parseInt(el.getAttribute('aria-level') || '1', 10);
        toc.push({ title: (a.textContent || '').trim(), href: a.getAttribute('href'), level: isNaN(lvl) ? 1 : lvl });
      }
    }
    // Article body (.slds-text-longform), reached through shadow DOM.
    let contentEl = null;
    for (const el of walk(document)) {
      if (el.classList && el.classList.contains('slds-text-longform')) { contentEl = el; break; }
    }
    let title = '';
    for (const el of walk(document)) { if (el.tagName === 'H1') { title = (el.textContent || '').trim(); break; } }
    return {
      toc,
      contentHtml: contentEl ? contentEl.innerHTML : '',
      contentText: contentEl ? (contentEl.innerText || '') : '',
      title,
    };
  });
  return { data, type: 'application/json' };
}
"""


# Browserless /function module that returns the fully-rendered light-DOM HTML
# after waiting for a selector to appear. Used by profiles whose nav/content is
# JS-rendered into the light DOM (e.g. Commvault's #nav), so a plain Firecrawl
# scrape catches it mid-"Loading…".
_HTML_FUNCTION_CODE = r"""
export default async function ({ page, context }) {
  const { url, waitMs, waitSelector } = context;
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  if (waitSelector) {
    // Generous timeout: client-rendered navs can be heavy (e.g. Commvault builds
    // its tree from a ~515KB manifest). waitForSelector returns as soon as it
    // appears, so this only caps the worst case.
    try { await page.waitForSelector(waitSelector, { timeout: 30000 }); }
    catch (e) { /* fall through with whatever rendered */ }
    await new Promise(r => setTimeout(r, 1500));
  } else {
    await new Promise(r => setTimeout(r, waitMs || 9000));
  }
  const html = await page.evaluate(() => document.documentElement.outerHTML);
  return { data: { html }, type: 'application/json' };
}
"""


# Browserless /function that traverses a lazy-loaded sidebar tree depth-first,
# clicking each parent's toggle to reveal its children, and returns the ordered
# nodes with depth. Mirrors the proven Commvault Playwright crawler: a SINGLE
# session loads the page once (the ~515KB nav render is the only expensive step)
# then expands the whole tree with cheap in-page clicks. Selectors:
# ul.nav-group-root (root) / ul.nav-group (children), li > div.nav-item with
# data-is-parent, a.nav-text.fetch-doc (link), button.nav-parent-toggle (expand).
#
# Expansion uses an in-page el.click() (via page.evaluate) rather than
# page.click(): the latter requires the toggle to be in the viewport and fails
# for nodes below the fold (the cause of earlier empty subtrees). We scroll the
# toggle into view, click it in-page, then poll for children — measured at
# ~200ms per node against the live site, reliably, for every section.
_TOC_EXPAND_CODE = r"""
export default async function ({ page, context }) {
  const { url, sectionId } = context;
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('div#nav > ul.nav-group-root', { timeout: 30000 });

  const out = [];
  const expanded = new Set();

  const readItems = (sel) => page.evaluate((s) => {
    const list = document.querySelector(s);
    if (!list) return [];
    return Array.from(list.children).filter(el => el.tagName === 'LI').map(li => {
      const navItem = li.querySelector(':scope > div.nav-item');
      const link = navItem ? navItem.querySelector('a.nav-text.fetch-doc') : null;
      return {
        id: li.id || '',
        href: link ? link.getAttribute('href') : null,
        title: (link ? link.textContent : (navItem ? navItem.textContent : '')).trim(),
        isParent: navItem ? navItem.hasAttribute('data-is-parent') : false,
      };
    });
  }, sel);

  // Top-level-only mode: list the root sections without expanding. The caller
  // (resumable build) then expands each section in its own checkpointed call.
  if (sectionId === '__TOP__') {
    const tops = (await readItems('div#nav > ul.nav-group-root')).map(it => ({
      id: it.id, href: it.href, title: it.title, level: 0, isParent: it.isParent,
    }));
    return { data: { toc: tops }, type: 'application/json' };
  }

  async function expand(id) {
    if (!id || expanded.has(id)) return false;
    expanded.add(id);
    const childSel = `li[id="${id}"] > ul.nav-group > li`;
    const clicked = await page.evaluate((sid) => {
      const li = document.getElementById(sid);
      if (!li) return false;
      const btn = li.querySelector(':scope > div.nav-item button.nav-parent-toggle');
      if (!btn) return false;
      btn.scrollIntoView({ block: 'center' });
      btn.click();
      return true;
    }, id);
    if (!clicked) return false;
    // Poll for the lazily-loaded children (cheap; usually ready within ~200ms).
    for (let i = 0; i < 40; i++) {
      if (await page.evaluate((s) => document.querySelectorAll(s).length, childSel) > 0) return true;
      await new Promise(r => setTimeout(r, 150));
    }
    return false;
  }

  async function processList(sel, depth) {
    for (const it of await readItems(sel)) {
      out.push({ id: it.id, href: it.href, title: it.title, level: depth, isParent: it.isParent });
      if (it.isParent && await expand(it.id)) {
        await processList(`li[id="${it.id}"] > ul.nav-group`, depth + 1);
      }
    }
  }

  if (sectionId) {
    const info = await page.evaluate((sid) => {
      const li = document.getElementById(sid); if (!li) return null;
      const ni = li.querySelector(':scope > div.nav-item');
      const a = ni ? ni.querySelector('a.nav-text.fetch-doc') : null;
      return { href: a ? a.getAttribute('href') : null,
               title: (a ? a.textContent : (ni ? ni.textContent : '')).trim(),
               isParent: ni ? ni.hasAttribute('data-is-parent') : false };
    }, sectionId);
    if (info) {
      out.push({ id: sectionId, href: info.href, title: info.title, level: 0, isParent: info.isParent });
      if (info.isParent && await expand(sectionId)) {
        await processList(`li[id="${sectionId}"] > ul.nav-group`, 1);
      }
    }
  } else {
    await processList('div#nav > ul.nav-group-root', 0);
  }
  return { data: { toc: out }, type: 'application/json' };
}
"""


# Browserless /function that visits each URL in one warm session and returns its
# table-of-contents <aside> HTML. GitBook renders a *contextual* sidebar: a
# node's children appear only when you navigate to that node, so the full tree is
# reconstructed by visiting every page and merging each one's revealed children.
_GITBOOK_SIDEBARS_CODE = r"""
export default async function ({ page, context }) {
  const sel = 'aside[data-testid="table-of-contents"]';
  const out = {};
  for (const url of context.urls) {
    try {
      await page.goto(url, { waitUntil: 'networkidle2', timeout: 45000 });
      await page.waitForSelector(sel, { timeout: 20000 });
      await new Promise(r => setTimeout(r, 700));
      out[url] = await page.evaluate((s) => {
        const a = document.querySelector(s);
        return a ? a.outerHTML : '';
      }, sel);
    } catch (e) { out[url] = ''; }
  }
  return { data: { sidebars: out }, type: 'application/json' };
}
"""


# Browserless /function that fully expands a Docusaurus sidebar and returns its
# HTML. Docusaurus does NOT mount a collapsed category's children in the DOM
# until it is expanded, so a single render only ever exposes the top level. We
# load the page once, then repeatedly click every collapsed caret
# (button.menu__caret[aria-expanded="false"]) — each click mounts that category's
# child <ul>, which may itself contain further collapsed carets — until none
# remain. In-page el.click() (not page.click) so below-the-fold toggles work,
# matching the Commvault crawler. Measured on Portworx: 11 → 258 links in 4
# rounds, links increasing monotonically (no auto-collapse fighting).
_DOCUSAURUS_EXPAND_CODE = r"""
export default async function ({ page, context }) {
  const { url } = context;
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('.theme-doc-sidebar-menu', { timeout: 30000 });
  let rounds = 0;
  while (rounds++ < 80) {
    const clicked = await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('button.menu__caret'))
        .filter(b => b.getAttribute('aria-expanded') === 'false');
      btns.forEach(b => { try { b.scrollIntoView({ block: 'center' }); b.click(); } catch (e) {} });
      return btns.length;
    });
    if (clicked === 0) break;
    // Let the expanded category mount its child <ul> (and any nested carets).
    await new Promise(r => setTimeout(r, 400));
  }
  const html = await page.evaluate(() => {
    const el = document.querySelector('.theme-doc-sidebar-menu');
    return el ? el.outerHTML : '';
  });
  return { data: { html }, type: 'application/json' };
}
"""


# Browserless /function that fully expands a shadcn/ui + radix Collapsible
# sidebar (used by docs.cohesity.com) and returns its HTML. Each guide/section is
# a radix Collapsible whose content (child <ul data-slot="sidebar-menu">) is NOT
# mounted in the DOM until its trigger is clicked, so a single render exposes only
# the top-level guides (observed: 74 guides → 10 links). We load the page once,
# then repeatedly click every collapsed trigger
# (button[data-slot="collapsible-trigger"][aria-expanded="false"]) — each click
# mounts that node's children, which may contain further collapsed triggers —
# until none remain. In-page el.click() (not page.click) so below-the-fold
# toggles fire. The round cap bounds by tree depth, not breadth.
_COHESITY_EXPAND_CODE = r"""
export default async function ({ page, context }) {
  const { url } = context;
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector("[data-slot='sidebar-inner']", { timeout: 30000 });
  let rounds = 0;
  while (rounds++ < 120) {
    const clicked = await page.evaluate(() => {
      const btns = Array.from(
        document.querySelectorAll("button[data-slot='collapsible-trigger']")
      ).filter(b => b.getAttribute('aria-expanded') === 'false');
      btns.forEach(b => { try { b.scrollIntoView({ block: 'center' }); b.click(); } catch (e) {} });
      return btns.length;
    });
    if (clicked === 0) break;
    // Let the expanded node mount its child <ul> (and any nested triggers).
    await new Promise(r => setTimeout(r, 400));
  }
  const html = await page.evaluate(() => {
    const el = document.querySelector("[data-slot='sidebar-inner']");
    return el ? el.outerHTML : '';
  });
  return { data: { html }, type: 'application/json' };
}
"""


class BrowserlessError(Exception):
    """Raised when Browserless is unreachable or returns an unusable response."""


class BrowserlessClient:
    """Minimal client over Browserless's ``/function`` API."""

    # Browserless returns a transient error when an in-page op fails (a page-load
    # or selector timeout inside the function surfaces as 400, or the instance is
    # briefly overloaded → 5xx, or the connection drops). Our request bodies are
    # always well-formed, so retry these rather than failing the whole TOC build
    # on one blip. A persistently malformed request still fails after the cap.
    TRANSIENT_STATUS = (400, 408, 429, 500, 502, 503, 504)
    TRANSIENT_RETRIES = 3
    TRANSIENT_BACKOFF = 5.0  # seconds; ×3 each attempt (5, 15, 45)

    def __init__(self, url: str | None = None, token: str | None = None, wait_ms: int | None = None):
        self.url = (url or settings.browserless_url).rstrip("/")
        self.token = token if token is not None else settings.browserless_token
        self.wait_ms = wait_ms or settings.browserless_wait_ms

    async def _post(self, code: str, context: dict, target_url: str,
                    client: httpx.AsyncClient | None = None,
                    session_timeout_ms: int | None = None,
                    http_timeout_s: float = 120.0) -> dict:
        """POST a /function call and return its unwrapped data dict.

        ``session_timeout_ms`` caps the Browserless session (``?timeout=``) — raise
        it for long jobs like full TOC expansion (lots of clicks). ``http_timeout_s``
        must exceed it so the HTTP read doesn't give up first.
        """
        endpoint = f"{self.url}/function"
        if session_timeout_ms:
            endpoint += f"?timeout={session_timeout_ms}"  # token stays in the header
        # Token as a Bearer header, not ?token=, so it doesn't leak into logs.
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        payload = {"code": code, "context": context}

        owns = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(http_timeout_s, connect=10.0))
        delay = self.TRANSIENT_BACKOFF
        try:
            body = None
            for attempt in range(self.TRANSIENT_RETRIES + 1):
                try:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                    resp.raise_for_status()
                    body = resp.json()
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code if exc.response is not None else None
                    if code not in self.TRANSIENT_STATUS or attempt >= self.TRANSIENT_RETRIES:
                        raise BrowserlessError(
                            f"Browserless request failed for {target_url}: {exc}"
                        ) from exc
                    reason = exc
                except httpx.TransportError as exc:  # connect/read/write/timeout
                    if attempt >= self.TRANSIENT_RETRIES:
                        raise BrowserlessError(
                            f"Browserless request failed for {target_url}: {exc}"
                        ) from exc
                    reason = exc
                except ValueError as exc:
                    raise BrowserlessError(
                        f"Browserless returned non-JSON for {target_url}: {exc}"
                    ) from exc
                logger.warning(
                    "Browserless %s transient failure (%s) — retry %d/%d in %.0fs",
                    target_url, reason, attempt + 1, self.TRANSIENT_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                delay *= 3
        finally:
            if owns:
                await client.aclose()

        # Browserless may return the function's value directly or wrapped in {data}.
        data = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(data, dict):
            raise BrowserlessError(f"Unexpected Browserless payload for {target_url}: {type(data)}")
        return data

    async def render(self, target_url: str, client: httpx.AsyncClient | None = None) -> dict:
        """Render ``target_url`` and return {toc, contentHtml, contentText, title}
        via shadow-DOM extraction (Salesforce Help). Raises BrowserlessError."""
        return await self._post(
            _FUNCTION_CODE, {"url": target_url, "waitMs": self.wait_ms}, target_url, client
        )

    async def render_html(self, target_url: str, wait_selector: str | None = None,
                          client: httpx.AsyncClient | None = None) -> str:
        """Return the fully-rendered light-DOM HTML after a JS-rendered element
        (``wait_selector``) appears — for navs/content built client-side into the
        light DOM (e.g. Commvault's #nav). Raises BrowserlessError."""
        data = await self._post(
            _HTML_FUNCTION_CODE,
            {"url": target_url, "waitMs": self.wait_ms, "waitSelector": wait_selector},
            target_url, client,
        )
        return data.get("html", "")

    async def expand_toc(self, target_url: str, section_id: str | None = None,
                         client: httpx.AsyncClient | None = None) -> list[dict]:
        """Depth-first expand a lazy sidebar tree and return ordered nodes.

        Each node is {href, title, level, isParent}. ``section_id`` scopes to one
        section's ``<li id>`` (else the whole nav-group-root). Uses a long
        Browserless session timeout since expansion clicks every parent.
        """
        timeout_ms = settings.browserless_toc_timeout_ms
        data = await self._post(
            _TOC_EXPAND_CODE,
            {"url": target_url, "sectionId": section_id},
            target_url, client,
            session_timeout_ms=timeout_ms,
            http_timeout_s=timeout_ms / 1000 + 30,
        )
        toc = data.get("toc")
        return toc if isinstance(toc, list) else []

    async def expand_docusaurus_sidebar(self, target_url: str,
                                        client: httpx.AsyncClient | None = None) -> str:
        """Fully expand a Docusaurus sidebar (clicking every collapsed category)
        and return the ``.theme-doc-sidebar-menu`` outerHTML with all children
        mounted. Raises BrowserlessError. Uses the long TOC session timeout since
        a deep tree means many click+mount rounds."""
        timeout_ms = settings.browserless_toc_timeout_ms
        data = await self._post(
            _DOCUSAURUS_EXPAND_CODE,
            {"url": target_url},
            target_url, client,
            session_timeout_ms=timeout_ms,
            http_timeout_s=timeout_ms / 1000 + 30,
        )
        return data.get("html", "")

    async def expand_collapsible_sidebar(self, target_url: str,
                                         client: httpx.AsyncClient | None = None) -> str:
        """Fully expand a shadcn/ui + radix Collapsible sidebar (docs.cohesity.com),
        clicking every collapsed ``collapsible-trigger`` until the whole tree is
        mounted, and return the ``[data-slot='sidebar-inner']`` outerHTML. Raises
        BrowserlessError. Uses the long TOC session timeout (deep tree → many
        click+mount rounds)."""
        timeout_ms = settings.browserless_toc_timeout_ms
        data = await self._post(
            _COHESITY_EXPAND_CODE,
            {"url": target_url},
            target_url, client,
            session_timeout_ms=timeout_ms,
            http_timeout_s=timeout_ms / 1000 + 30,
        )
        return data.get("html", "")

    async def gitbook_sidebars(self, urls: list[str],
                               client: httpx.AsyncClient | None = None) -> dict[str, str]:
        """Visit each URL in one session and return {url: table-of-contents HTML}.

        Used to reconstruct a GitBook tree: each page reveals its own node's
        direct children in the sidebar. Uses the long TOC session timeout since a
        batch can be dozens of navigations. Raises BrowserlessError.
        """
        if not urls:
            return {}
        timeout_ms = settings.browserless_toc_timeout_ms
        data = await self._post(
            _GITBOOK_SIDEBARS_CODE,
            {"urls": urls},
            urls[0], client,
            session_timeout_ms=timeout_ms,
            http_timeout_s=timeout_ms / 1000 + 30,
        )
        sidebars = data.get("sidebars")
        return sidebars if isinstance(sidebars, dict) else {}


browserless_client = BrowserlessClient()
