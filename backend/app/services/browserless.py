"""Browserless client — a real Chrome that can run JS in the page.

Some documentation platforms (e.g. Salesforce Help, a Lightning Web Components
SPA) render their TOC and article body entirely inside **shadow DOM**. Firecrawl
serialises only the light DOM, so it returns a near-empty shell. Browserless's
``/function`` endpoint runs arbitrary JS in the rendered page, letting us pierce
shadow DOM and extract both the navigation tree and the article content.

The same Browserless instance already backs Firecrawl's engine, so the page
renders identically; we only need a different *extraction* path.
"""

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


class BrowserlessError(Exception):
    """Raised when Browserless is unreachable or returns an unusable response."""


class BrowserlessClient:
    """Minimal client over Browserless's ``/function`` API."""

    def __init__(self, url: str | None = None, token: str | None = None, wait_ms: int | None = None):
        self.url = (url or settings.browserless_url).rstrip("/")
        self.token = token if token is not None else settings.browserless_token
        self.wait_ms = wait_ms or settings.browserless_wait_ms

    async def _post(self, code: str, context: dict, target_url: str,
                    client: httpx.AsyncClient | None = None) -> dict:
        """POST a /function call and return its unwrapped data dict."""
        endpoint = f"{self.url}/function"
        # Token as a Bearer header, not ?token=, so it doesn't leak into logs.
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        payload = {"code": code, "context": context}

        owns = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        try:
            resp = await client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise BrowserlessError(f"Browserless request failed for {target_url}: {exc}") from exc
        except ValueError as exc:
            raise BrowserlessError(f"Browserless returned non-JSON for {target_url}: {exc}") from exc
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


browserless_client = BrowserlessClient()
