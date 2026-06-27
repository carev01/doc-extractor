"""Generic static-HTML content scoping for the ``raw_http`` content engine.

Profiles whose article body is served as static HTML can opt into the raw_http
path (``_scrape_via_raw_http`` in ``services/firecrawl.py``) *without* a bespoke
extractor: this helper scopes the body using the include/exclude selectors the
profile already declares in ``content_config()``. Profiles with non-trivial
needs (e.g. ``flare_webhelp``) keep their own ``extract_content_html`` and skip
this.

Semantics mirror Firecrawl's ``includeTags``/``excludeTags``: keep the **union**
of every element matching any include selector (outermost wins, so a nested
match isn't double-counted), then drop every element matching an exclude
selector (including a matched root — e.g. a sidebar that shares the include
class). This matters for profiles like ``category_accordion`` whose body is an
``<article>`` plus sibling ``.m.embed`` table blocks outside it.

Selector handling deliberately avoids soupsieve for attribute and id selectors.
soupsieve's module-level compiled-selector cache proved unreliable for
attribute-presence selectors (``[data-mc-content-body]``) under the full test
suite — see PR #62 — so those use bs4-native ``find_all(attrs=...)`` /
``find_all(id=...)``. Class/element/compound selectors (CI-safe) use soupsieve.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# A single attribute selector: ``[name]`` (presence) or ``[name=value]`` /
# ``[name="value"]`` (exact). Fancy operators (~=, ^=, *=, …) defer to soupsieve.
_ATTR_RE = re.compile(r'^\[([A-Za-z_:][-\w:.]*)(?:=\s*"?([^"\]]*)"?)?\]$')
_ID_RE = re.compile(r"^#([-\w]+)$")


def _find_all(scope, selector: str) -> list:
    """All elements matching ``selector``. Attribute/id selectors use bs4-native
    lookups (cache-safe); everything else defers to soupsieve ``select``."""
    selector = selector.strip()
    m = _ATTR_RE.match(selector)
    if m:
        name, value = m.group(1), m.group(2)
        return scope.find_all(attrs={name: True if value is None else value})
    m = _ID_RE.match(selector)
    if m:
        return scope.find_all(id=m.group(1))
    return scope.select(selector)


def strip_selectors(html: str, selectors: list[str] | None) -> str:
    """Drop every element matching any of ``selectors`` from ``html``.

    Exclude-only counterpart to :func:`scope_content_html`, for the Browserless
    content path (which already has the scoped article innerHTML, so it only
    needs chrome removed — e.g. a Red Hat chapter's ``nav.pagination`` PreviousNext
    footer and ``.copy-link-tooltip`` per-heading copy widgets). Returns ``html``
    unchanged when there is nothing to do.
    """
    if not html or not selectors:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for sel in selectors:
        for el in _find_all(soup, sel):
            el.decompose()
    return str(soup)


def scope_content_html(
    raw_html: str,
    url: str,
    include_selectors: list[str],
    exclude_selectors: list[str] | None = None,
) -> str | None:
    """Scope an article body out of statically-served HTML.

    Returns the concatenated HTML of the kept subtrees, or ``None`` when no
    include selector matches (so the caller can skip/flag the page). Relative
    ``<img src>`` values are absolutised against ``url`` so the downstream image
    download/rewrite step can match them.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # Collect the union of include matches, keeping only outermost (drop a match
    # nested inside another match), de-duplicated by identity.
    matched: list = []
    seen: set[int] = set()
    for sel in include_selectors:
        for el in _find_all(soup, sel):
            if id(el) in seen:
                continue
            seen.add(id(el))
            matched.append(el)
    if not matched:
        return None

    matched_ids = {id(el) for el in matched}
    outermost = [
        el for el in matched
        if not any(id(p) in matched_ids for p in el.parents)
    ]

    # Move the kept subtrees into a fresh fragment so excludeTags can drop a
    # matched root (not just descendants), mirroring Firecrawl's two-pass apply.
    frag = BeautifulSoup("<div></div>", "html.parser")
    root = frag.div
    for el in outermost:
        root.append(el.extract())

    for sel in exclude_selectors or ():
        for ex in _find_all(root, sel):
            ex.decompose()

    for img in root.find_all("img"):
        src = img.get("src")
        if src:
            img["src"] = urljoin(url, src)

    inner = root.decode_contents().strip()
    return inner or None
