"""Reusable TOC-acquisition strategies shared by platform profiles.

- sidebar_tree_toc: parse a nested <ul>/<li><a> nav into an ordered TOC.
- hubspoke_toc: crawl root -> categories -> (sections) -> articles (help centers).
- sitemap_urls: enumerate URLs from sitemap.xml in document order.
- flare_helpsystem_toc: build a MadCap Flare TOC from its Data/ files (the TOC is
  rendered client-side and never present in the page HTML).
"""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import TocEntry


async def sidebar_tree_toc(
    scraper, root_url: str, nav_selector: str, *, item_selector: str = "a", wait_ms: int = 1500
) -> list[TocEntry]:
    """Parse the nested list under ``nav_selector`` into an ordered TOC.

    Pass the nav container element OR the list (<ul>) itself — if the selected
    element is already a ``<ul>``, it is used directly as the top-level list.

    A node with a child <ul> is treated as a section (is_article=False); a leaf
    link is an article. Order is the DOM order of the nav.
    """
    soup = BeautifulSoup(await scraper.get_html(root_url, wait_ms), "html.parser")
    nav = soup.select_one(nav_selector)
    out: list[TocEntry] = []
    if not nav:
        return out

    def walk(ul, level: int, parent_url: str | None) -> None:
        for li in ul.find_all("li", recursive=False):
            a = li.find(item_selector)
            # Prefer a direct child <ul>; fall back to any descendant <ul> to
            # handle wrappers like MkDocs Material's <li><nav><ul>…</ul></nav></li>.
            child_ul = li.find("ul", recursive=False) or li.find("ul")
            if not a or not a.get("href"):
                # Section label without its own link: descend, keeping the parent.
                if child_ul:
                    walk(child_ul, level, parent_url)
                continue
            url = urljoin(root_url, a["href"])
            out.append(TocEntry(
                title=a.get_text(strip=True), url=url, level=level,
                is_article=child_ul is None, parent_url=parent_url,
            ))
            if child_ul:
                walk(child_ul, level + 1, url)

    top = nav if nav.name == "ul" else nav.find("ul")
    if top:
        walk(top, 0, None)
    return out


def _element_title(element, title_selector: str | None) -> str:
    """Extract a clean title from *element*.

    When *title_selector* is given, find the first matching sub-element and
    return its text.  Falls back to the element's own full text if the
    sub-element is not found.  When *title_selector* is None, returns the
    element's full text (original behaviour).
    """
    if title_selector:
        sub = element.select_one(title_selector)
        if sub:
            return sub.get_text(strip=True)
    return element.get_text(strip=True)


async def hubspoke_toc(
    scraper, root_url: str, *, category_link_selector: str, article_link_selector: str,
    section_link_selector: str | None = None,
    category_title_selector: str | None = None,
    article_title_selector: str | None = None,
) -> list[TocEntry]:
    """Crawl a help-center hub: root -> categories -> (optional sections) -> articles.

    *category_title_selector* and *article_title_selector* are optional CSS
    selectors scoped to the matched link element.  When provided, the title is
    extracted from the first matching sub-element instead of the link's full
    text.  This avoids description-concatenation issues (e.g. Intercom, where
    ``get_text`` on a collection anchor yields "Title + Description + count").
    Defaults are None, which preserves the original behaviour.
    """
    root = BeautifulSoup(await scraper.get_html(root_url), "html.parser")
    out: list[TocEntry] = []
    seen: set[str] = set()
    for cat in root.select(category_link_selector):
        if not cat.get("href"):
            continue
        cat_url = urljoin(root_url, cat["href"])
        if cat_url in seen:
            continue
        seen.add(cat_url)
        cat_title = _element_title(cat, category_title_selector)
        out.append(TocEntry(cat_title, cat_url, 0, False, None))
        cat_soup = BeautifulSoup(await scraper.get_html(cat_url), "html.parser")

        if section_link_selector:
            sections = [(s.get_text(strip=True), urljoin(cat_url, s["href"]))
                        for s in cat_soup.select(section_link_selector) if s.get("href")]
        else:
            sections = [(None, cat_url)]

        for sec_title, sec_url in sections:
            if sec_title is None:
                sec_soup, parent, alevel = cat_soup, cat_url, 1
            else:
                if sec_url in seen:
                    continue
                seen.add(sec_url)
                out.append(TocEntry(sec_title, sec_url, 1, False, cat_url))
                sec_soup = BeautifulSoup(await scraper.get_html(sec_url), "html.parser")
                parent, alevel = sec_url, 2
            for art in sec_soup.select(article_link_selector):
                if not art.get("href"):
                    continue
                art_url = urljoin(sec_url, art["href"])
                if art_url in seen:
                    continue
                seen.add(art_url)
                art_title = _element_title(art, article_title_selector)
                out.append(TocEntry(art_title, art_url, alevel, True, parent))
    return out


async def _try_get_raw(scraper, url: str) -> str | None:
    """Best-effort verbatim GET; None on any error (incl. 404)."""
    try:
        txt = await scraper.get_raw(url)
        return txt or None
    except Exception:
        return None


def _js_unescape(s: str) -> str:
    """Decode JS string-literal escapes (\\uXXXX, \\', \\", \\\\, \\n, …) in one pass."""
    def repl(m: "re.Match") -> str:
        esc = m.group(1)
        if esc[0] == "u":
            return chr(int(esc[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", repl, s)


def _parse_flare_tree(master: str) -> dict | None:
    """Extract the numeric ``tree:{...}`` object from a Flare master TOC.

    Nodes use bare keys — ``i`` (index), ``c`` (chunk), ``n`` (children) plus
    build-specific extras some Flare versions add (``w``, and ``f`` whose value
    is a single-quoted string like ``'_self'``). We JSON-ify by converting
    single-quoted string values to double-quoted and quoting every bare key; the
    walk only reads i/c/n, so the extras are harmless once it parses.
    """
    i = master.find("tree:")
    if i == -1:
        return None
    j = master.find("{", i)
    if j == -1:
        return None
    depth = 0
    blob = None
    for k in range(j, len(master)):
        if master[k] == "{":
            depth += 1
        elif master[k] == "}":
            depth -= 1
            if depth == 0:
                blob = master[j:k + 1]
                break
    if blob is None:
        return None
    def _to_json_str(m: "re.Match") -> str:
        s = _js_unescape(m.group(1))
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    # JS → JSON: single-quoted string values → double-quoted, then quote bare keys.
    jsonish = re.sub(r"'((?:[^'\\]|\\.)*)'", _to_json_str, blob)
    jsonish = re.sub(r'([{,\[])\s*([A-Za-z_]\w*)\s*:', r'\1"\2":', jsonish)
    try:
        return json.loads(jsonish)
    except (ValueError, TypeError):
        return None


_CHUNK_ENTRY_RE = re.compile(
    r"'((?:[^'\\]|\\.)*)'\s*:\s*\{i:\[([\d,\s]+)\]\s*,\s*"
    r"t:\[((?:'(?:[^'\\]|\\.)*'\s*,?\s*)+)\]"
)
_CHUNK_TITLE_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _parse_flare_chunk(text: str) -> dict[int, tuple[str, str]]:
    """Parse a Flare TOC chunk: ``'<href>':{i:[idx,...],t:['<title>',...],...}``.

    A page that appears at several TOC positions carries parallel index/title
    lists (``i:[679,724],t:['A','B']``), so each index maps to its matching
    title. Returns {tree_index: (href, title)}.
    """
    out: dict[int, tuple[str, str]] = {}
    for m in _CHUNK_ENTRY_RE.finditer(text):
        href = _js_unescape(m.group(1))
        indices = [int(x) for x in m.group(2).split(",") if x.strip().isdigit()]
        titles = [_js_unescape(t) for t in _CHUNK_TITLE_RE.findall(m.group(3))]
        for k, idx in enumerate(indices):
            title = titles[k] if k < len(titles) else (titles[0] if titles else "")
            out[idx] = (href, title)
    return out


async def flare_helpsystem_toc(scraper, root_url: str) -> list[TocEntry]:
    """Build a MadCap Flare TOC from its static ``Data/`` files.

    Flare renders the TOC client-side from data files (``Data/HelpSystem.xml``
    names the TOC; the master TOC defines the tree and references chunk files
    holding each entry's href+title), so the TOC never appears in the page HTML.
    We fetch and resolve those files into an ordered, hierarchical TOC.

    Returns [] when the data files aren't web-served (older/server-side Flare
    outputs), so callers can fall back to parsing the rendered nav.
    """
    # Locate the help-system root, which holds Data/HelpSystem.xml. Layout varies:
    #   - HTML5 Side Nav: topics live under <root>/Content/… (often nested
    #     several levels deep, e.g. Content/kb/siris-alto-nas/foo.htm), so the
    #     root is the path up to "/Content/", NOT just one directory up.
    #   - WebHelp/TriPane: the entry is default.htm sitting AT the root.
    # Try the Content-split root first, then the document's own dir and parent.
    candidates: list[str] = []
    low = root_url.lower()
    idx = low.find("/content/")
    if idx != -1:
        candidates.append(root_url[:idx + 1])  # ".../help/" (parent of /Content/)
    for rel in ("./", "../"):
        cand = urljoin(root_url, rel)
        if cand not in candidates:
            candidates.append(cand)

    help_root = None
    hs_xml = None
    for candidate in candidates:
        xml = await _try_get_raw(scraper, urljoin(candidate, "Data/HelpSystem.xml"))
        if xml:
            help_root, hs_xml = candidate, xml
            break
    if not hs_xml:
        return []
    m = re.search(r'\bToc="([^"]+)"', hs_xml)
    if not m:
        return []
    master_url = urljoin(help_root, m.group(1))
    master = await _try_get_raw(scraper, master_url)
    if not master:
        return []

    tree = _parse_flare_tree(master)
    prefix_m = re.search(r"prefix:'([^']*)'", master)
    if tree is None or not prefix_m:
        return []
    prefix = prefix_m.group(1)
    numchunks_m = re.search(r"numchunks:(\d+)", master)
    numchunks = int(numchunks_m.group(1)) if numchunks_m else 1

    chunks: dict[int, dict[int, tuple[str, str]]] = {}
    for c in range(numchunks):
        txt = await _try_get_raw(scraper, urljoin(master_url, f"{prefix}{c}.js"))
        chunks[c] = _parse_flare_chunk(txt) if txt else {}

    out: list[TocEntry] = []

    def walk(nodes: list, level: int, parent_url: str | None) -> None:
        for node in nodes:
            idx = node.get("i")
            entry = chunks.get(node.get("c", 0), {}).get(idx)
            kids = node.get("n")
            if entry is None:
                # Container with no resolvable entry: descend, keep the parent.
                if kids:
                    walk(kids, level, parent_url)
                continue
            href, title = entry
            # MadCap uses an all-underscores placeholder href ("___") for
            # container/"book" TOC nodes that have no page of their own. Treat
            # them as structural sections: no URL, so they are never scraped and
            # — crucially — never collapsed together by URL-dedup (otherwise
            # every such section merges into one and the tree scrambles). Their
            # children then link by level adjacency to the distinct section.
            is_placeholder = (not href) or set(href) <= {"_"}
            url = None if is_placeholder else urljoin(help_root, href.lstrip("/"))
            out.append(TocEntry(
                title=title, url=url, level=level,
                is_article=bool(url) and not kids, parent_url=parent_url,
            ))
            if kids:
                walk(kids, level + 1, url)

    walk(tree.get("n", []), 0, None)
    return out


async def sitemap_urls(scraper, root_url: str) -> list[str]:
    """Return all <loc> URLs from the site's sitemap.xml, in document order."""
    parsed = urlparse(root_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    xml = await scraper.get_html(urljoin(base + "/", "sitemap.xml"))
    soup = BeautifulSoup(xml, "html.parser")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")]
