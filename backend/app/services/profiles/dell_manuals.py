"""Dell manual PDFs (``dl.dell.com/content/manual<id>-<slug>.pdf``).

Dell serves each guide as a PDF whose ``manual<id>`` is the file's only identity:
the slug after it is cosmetic (any slug, even one naming the wrong release, serves
the same bytes) while a wrong id 404s. Dell mints a new id for every release of
every guide, so a version bump can't derive it — ``manual25955871`` is Avamar
19.12's Administration Guide and nothing about 19.13's is predictable from it. The
id is therefore this platform's ``{rev}``, and the slug's release is
``{version_dashed}`` (Dell writes 19.12 as ``19-12`` there).

What *is* derivable is the guide's manual page,
``www.dell.com/support/manuals/en-us/<product>/<doc code>_<version>``, which links
the current PDF. The doc code isn't recoverable from the PDF URL, so each source
stores its page as a template in ``profile_config["dell_manual_page"]`` and
``resolve_revision`` reads the English PDF's id off that page. Two pitfalls,
both measured from our egress on 2026-11-05:

* Akamai fronts www.dell.com and fingerprints the client: Python's TLS/HTTP-2
  handshake gets 403 "Access Denied" with any headers (httpx, h1 or h2), while
  real Chrome is served. So the page is fetched through Browserless, which also
  means a bump-time lookup is one render per source.
* Our egress geolocates to Brazil, so a page without ``?lang=en-us`` is the
  pt-br edition and links the Portuguese PDF (a different id). Only a link whose
  own query says ``language=en-us`` is accepted.

PDF sources never reach TOC discovery, so this profile is only a ``{rev}``
resolver: it is pinned per source and never auto-detected.
"""

import html as html_lib
import logging
import re

from app.services.browserless import BrowserlessClient
from app.services.profiles import registry
from app.services.versioning import resolve_template

logger = logging.getLogger(__name__)

_PDF_URL_RE = re.compile(
    r"^(?P<head>https://dl\.dell\.com/content/)(?P<rev>manual\d+)-(?P<slug>[^?#]+)(?P<tail>.*)$"
)
_PAGE_LINK_RE = re.compile(
    r"https://dl\.dell\.com/content/(manual\d+)[^\"'<>\s]*?[?&]language=en-us"
)


async def _fetch_page(url: str) -> str | None:
    """Render a www.dell.com manual page in real Chrome (see module doc)."""
    return await BrowserlessClient().dom_html(url)


class DellManualsProfile:
    name = "dell_manuals"

    def detect(self, root_html: str, root_url: str) -> bool:
        return False

    async def build_toc(self, root_url: str, scraper) -> list:
        return []

    def content_config(self) -> dict:
        return {}

    def templatize_url(self, url: str, version: str) -> str | None:
        """Mark the PDF's id as ``{rev}`` and its slug's release as
        ``{version_dashed}``; None unless the slug carries *version*."""
        m = _PDF_URL_RE.match(url or "")
        dashed = re.sub(r"[^0-9A-Za-z]+", "-", version or "")
        if not m or not dashed:
            return None
        slug = m.group("slug")
        hit = re.search(rf"(?<![0-9]){re.escape(dashed)}(?![0-9])", slug)
        if not hit:
            return None
        slug = slug[:hit.start()] + "{version_dashed}" + slug[hit.end():]
        return f"{m.group('head')}{{rev}}-{slug}{m.group('tail')}"

    async def resolve_revision(self, template: str, version: str, scraper,
                               source=None) -> str | None:
        """Read the current English PDF id off the source's manual page at
        *version*. None (never a guess) when the source has no page configured,
        the page doesn't answer, or it links no English PDF."""
        page = ((getattr(source, "profile_config", None) or {})
                .get("dell_manual_page"))
        if not page or not version:
            return None
        url = resolve_template(page, version) + "?lang=en-us"
        try:
            text = await _fetch_page(url)
        except Exception as exc:
            logger.warning("Dell manual page %s failed: %s", url, exc)
            return None
        m = _PAGE_LINK_RE.search(html_lib.unescape(text or ""))
        return m.group(1) if m else None


PROFILE = DellManualsProfile()
registry.register(PROFILE)
