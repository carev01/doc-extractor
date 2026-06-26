"""MadCap Flare HTML5 (Side Navigation) documentation profile.

MadCap Flare produces two distinct HTML5 output formats:
  - Side Navigation (this profile): a self-contained SPA with a ``<ul
    class="... sidenav">`` accordion in the left sidebar.  The page title and
    nav are rendered server-side; deeper TOC branches are lazy-loaded via
    JS chunk requests as the user expands accordion items.
  - WebHelp / TriPane (flare_webhelp, a later task): a frame-based layout
    with ``<iframe id="topic">`` and a separate frame for the TOC tree.

STATIC-FIXTURE LIMITATION — lazy TOC nesting:
    Flare's sidenav only renders the top-level TOC items in the initial HTML.
    Sub-items are fetched on demand from ``Data/Toc.xml`` chunk files as the
    user expands branches.  Consequently ``build_toc`` over a static (or
    Firecrawl-scraped) page returns only the top-level entries.  Deeper
    nesting is a known runtime limitation and is not attempted here.

Content is scoped to the topic body ``[role=main]`` (``<div role="main"
id="mc-main-content">``), which MadCap emits on every Flare HTML5 topic — the
same selector ``flare_webhelp`` uses. We deliberately do NOT scope the broader
``[data-mc-content-body]`` wrapper: some custom skins (e.g. N-able's Cove Data
Protection) nest the mobile nav + search bar *inside* that wrapper, so scoping it
leaks site chrome into the article. ``[role=main]`` is the tight, chrome-free
container across all observed Flare HTML5 skins.

Detection guard:
    ``'ul class="... sidenav"`` is unique to the HTML5 Side Navigation skin.
    The WebHelp variant does NOT contain "sidenav" in the class attribute — it
    uses a plain ``<ul class="tree">`` inside a framed layout (``<iframe
    id="topic">``).  The two variants are therefore cleanly separated by the
    ``'sidenav' in root_html`` check alone; no additional iframe guard is
    needed, but we include it defensively.
"""

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry
from app.services.profiles.strategies import flare_helpsystem_toc, sidebar_tree_toc


class FlareHtml5Profile:
    name = "flare_html5"
    # Topic bodies are static server-rendered HTML scoped by
    # [data-mc-content-body] (see content_config); fetch them directly rather
    # than rendering. The generic scoper in _scrape_via_raw_http uses this
    # profile's content_config selectors.
    content_engine = "raw_http"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Return True for MadCap Flare HTML5 Side Navigation output.

        Required markers:
          - A MadCap/Flare marker: ``MadCap`` namespace, ``data-mc-`` attributes,
            or ``mc-`` CSS class prefix.
          - The sidenav element: ``sidenav`` appears in a class attribute
            (``class="... sidenav ..."``) — specific to the Side Nav skin.
          - NOT the frame-based WebHelp variant (which has ``<iframe id="topic"``
            and does not contain sidenav anyway, but we guard explicitly).
        """
        has_madcap = "MadCap" in root_html or "data-mc-" in root_html or ' mc-' in root_html
        has_sidenav = 'sidenav' in root_html
        is_webhelp = '<iframe id="topic"' in root_html
        return has_madcap and has_sidenav and not is_webhelp

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Parse the ``.sidenav`` accordion into an ordered TOC.

        Flare's sidenav anchors may contain a ``<span class="invisible-label">``
        that duplicates the visible text (used for accessibility toggle buttons).
        We strip those spans before walking the tree so titles are clean.

        Prefer the full TOC from Flare's static ``Data/`` files (HelpSystem.xml ->
        master TOC -> chunks); the rendered sidenav holds only top-level entries
        because deeper branches lazy-load via JS. Fall back to parsing the sidenav
        when those data files aren't web-served (older/server-side Flare outputs).
        """
        entries = await flare_helpsystem_toc(scraper, root_url)
        if entries:
            return entries

        html = await scraper.get_html(root_url, 1500)
        # Remove invisible-label spans that Flare injects inside <a> tags to
        # duplicate the link text for screen-reader toggle buttons.
        soup = BeautifulSoup(html, "html.parser")
        for span in soup.select(".invisible-label"):
            span.decompose()
        clean_html = str(soup)

        # Re-use sidebar_tree_toc via a one-shot fake scraper serving the
        # cleaned HTML — avoids duplicating the walk logic.
        from app.services.profiles.scraper import FakeScraper
        fake = FakeScraper({root_url: clean_html})
        return await sidebar_tree_toc(fake, root_url, ".sidenav")

    def content_config(self) -> dict:
        return {
            # Topic body, chrome-free. NOT [data-mc-content-body]: that wrapper
            # holds the mobile nav + search on some skins (N-able Cove), which
            # would leak into the article. scope_content_html keeps the OUTERMOST
            # of the union, so listing both would let the broader wrapper win —
            # hence role=main alone. Matches flare_webhelp's content scope.
            "includeTags": ["[role=main]"],
            # Drop Flare skin chrome before markdown conversion: the back-to-top
            # button, the "Was this article helpful?" feedback buttons, and any
            # element MadCap explicitly marks non-content. Post-processing
            # (services/sanitize.py) mops up textual residue like the footer.
            "excludeTags": [".GoToTop", ".feedback-button", ".nocontent"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = FlareHtml5Profile()
registry.register(PROFILE)
