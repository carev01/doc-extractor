"""MadCap Flare WebHelp / TriPane (frame-based) documentation profile.

This is the older, frame-based Flare output (as opposed to the modern HTML5
Side Navigation skin handled by ``flare_html5``).  Layout:

  - The index page (``default.htm``) is a **frameset shell**: the actual topic
    content is loaded into ``<iframe id="topic">``.  The shell itself has no
    article content — only navigation chrome.
  - The Table of Contents lives in an inline ``<ul class="tree"
    data-mc-chunk="Data/Toc.xml">`` inside ``<section id="toc">``.  Flare
    renders the **top-level** TOC nodes into this list server-side; deeper
    branches are lazy-loaded from ``Data/Toc.xml`` chunk files via JS only when
    the user expands an accordion node.
  - TOC anchors are hash-routed: ``default.htm#<TopicPath>?TocPath=...`` — the
    real topic page is the fragment (``<TopicPath>``), resolved relative to the
    help root, with the ``?TocPath`` routing query stripped.

TOC ACQUISITION — why we parse the inline tree (not Toc.xml or the sitemap):
    The documented ``data-mc-chunk="Data/Toc.xml"`` source is the canonical
    Flare TOC, but for the reference system (Arcserve UDP 10.0) every candidate
    location (``Data/Toc.xml``, ``Data/Tocs/Toc.xml``, chunk files, ``Toc.js``)
    returns HTTP 404 even through Firecrawl's stealth/rendering engine — the XML
    data simply is not web-served on this host.  The domain ``sitemap.xml``
    returns 200 but contains **zero** URLs under the help-system path prefix, so
    it is useless as an ordering source here.

    What *is* reliably available is the rendered index page: Firecrawl returns
    the full top-level ``<ul class="tree">`` (every ``<li data-mc-id>`` node)
    with clean titles and resolvable topic links.  We therefore build the TOC by
    parsing that inline tree.  This is the most robust path that works offline
    and deterministically against the captured fixture.

STATIC / RENDER LIMITATION — lazy TOC nesting:
    Only the top-level TOC entries are present in the rendered HTML.  Sub-topics
    are fetched on demand from Toc.xml chunks as the user expands branches, which
    neither a static fetch nor Firecrawl (which does not click the accordion)
    triggers.  ``build_toc`` therefore returns the top-level chapters/topics
    only.  Collapsed nodes (chapters with hidden children) are emitted as
    sections (``is_article=False``); leaf nodes are emitted as articles.  This
    is a known best-effort limitation for this frame-based skin.

CONTENT SCOPING:
    Topic pages must be scraped directly (never the frameset shell).  We follow
    the same config shape as the HTML5 skin — ``includeTags:
    [data-mc-content-body]`` — even though the older WebHelp topics on the
    reference host render a bare ``<body>`` without that attribute; in that case
    ``includeTags`` is a no-op and Firecrawl returns the page body, which is the
    desired best-effort behaviour.
"""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


class FlareWebHelpProfile:
    name = "flare_webhelp"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True for the frame-based MadCap Flare WebHelp / TriPane skin.

        Required markers:
          - A MadCap/Flare marker (``MadCap`` namespace, ``data-mc-`` attribute,
            or ``mc-`` CSS class prefix), AND
          - the frameset content host ``<iframe id="topic"`` — unique to the
            frame-based layout and absent from the HTML5 Side Nav skin.

        These two together separate this profile from ``flare_html5`` (which has
        MadCap markers but no ``<iframe id="topic">``) and from all non-Flare
        platforms (which have no MadCap markers).
        """
        has_madcap = "MadCap" in root_html or "data-mc-" in root_html or " mc-" in root_html
        is_frameset = '<iframe id="topic"' in root_html
        return has_madcap and is_frameset

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Parse the inline ``<ul class="tree">`` TOC into an ordered list.

        Each top-level ``<li>`` carries a ``tree-node-leaf`` (article) or
        ``tree-node-collapsed`` (section with lazy children) class.  The anchor
        href is hash-routed; we take the fragment as the topic path and resolve
        it against the help root (the index URL with ``default.htm`` stripped),
        discarding the ``?TocPath`` routing query.
        """
        html = await scraper.get_html(root_url, 1500)
        soup = BeautifulSoup(html, "html.parser")
        # Help root = the directory containing default.htm (e.g. .../SolG/).
        help_root = urljoin(root_url, ".")

        tree = soup.select_one("ul.tree")
        out: list[TocEntry] = []
        if not tree:
            return out

        for li in tree.find_all("li", recursive=False):
            a = li.find("a")
            if not a or not a.get("href"):
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            url = self._resolve_topic_url(a["href"], help_root)
            # A collapsed node is a chapter/section with (lazy) children.
            classes = li.get("class", [])
            is_section = "tree-node-collapsed" in classes
            out.append(TocEntry(
                title=title, url=url, level=0,
                is_article=not is_section, parent_url=None,
            ))
        return out

    @staticmethod
    def _resolve_topic_url(href: str, help_root: str) -> str:
        """Resolve a hash-routed Flare TOC href to an absolute topic URL.

        ``default.htm#UDPSolnGuide/foo.htm?TocPath=_____1`` -> ``<help_root>/UDPSolnGuide/foo.htm``
        A href without a fragment is resolved as a plain relative link.
        """
        if "#" in href:
            _, fragment = href.split("#", 1)
        else:
            fragment = href
        # Drop Flare's ?TocPath=... routing query — not part of the topic path.
        topic_path = fragment.split("?", 1)[0]
        return urljoin(help_root, topic_path)

    def content_config(self) -> dict:
        return {
            "includeTags": ["[data-mc-content-body]"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = FlareWebHelpProfile()
registry.register(PROFILE)
