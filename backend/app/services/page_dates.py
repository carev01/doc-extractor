"""Vendor-declared revision dates, with provenance.

``Article.last_updated_at`` is the *vendor's* claim about when a page changed, as
distinct from ``content_changed_at`` (when the text we serve changed) and
``extracted_at`` (when we crawled). It stays NULL when a page declares nothing —
a fabricated date is worse than an honest null, because it invites trust.

``last_updated_source`` records where the value came from, since the tiers
justify different downstream actions. A consumer that expires facts on a date
needs to know whether the vendor asserted it or we inferred it.

Two tiers are implemented, both being the vendor asserting a date in a
machine-readable field:

    vendor_meta   a <meta> revision date in <head>, or schema.org
                  ``dateModified`` in a JSON-LD block
    page_markup   a <time datetime> element in the article body

``page_markup`` is deliberately distinct from a ``page_text`` tier (a date
scraped out of visible prose, e.g. a footer "Last updated" line), which is a
materially weaker signal and which we do not implement. Conflating them would
make the distinction unrecoverable downstream.

Two signals are deliberately NOT used:

    HTTP Last-Modified   measured to return the *current day* for AWS docs — CDN
                         cache behaviour, not a revision. It would stamp every
                         page as changed daily, wearing a provenance label that
                         invites trust. Measured on Microsoft Learn too, where it
                         disagreed with the page's own date by four months.
    sitemap lastmod      not fetched; unknown whether vendors emit a revision
                         date or a generation date there.
"""

import json
from datetime import datetime

from bs4 import BeautifulSoup

# Provenance tiers, most trusted first.
SOURCE_VENDOR_META = "vendor_meta"
SOURCE_PAGE_MARKUP = "page_markup"

# <meta> names/properties carrying a revision date, in descending trust.
# The first three are cross-vendor standards; ms.date is Microsoft Learn's
# author-set editorial date, which is what that site shows readers as the page's
# last update. Microsoft also exposes a non-standard `updated_at` with minute
# precision, deliberately not read here: it tracks republication (it is always
# at or after ms.date, and moves for tooling-only rebuilds), whereas
# last_updated_at means the vendor's claim about the *content*.
_META_KEYS = (
    "article:modified_time",
    "og:updated_time",
    "dcterms.modified",
    "ms.date",
    "datemodified",
)


def _parse(value: str | None) -> datetime | None:
    """Parse an ISO-8601 date or datetime, or return None.

    Accepts a trailing ``Z`` (fromisoformat rejects it before 3.11) and a bare
    ``YYYY-MM-DD``, which several vendors emit — those land at midnight, so the
    value is day-granular and cannot order two changes within the same day.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _from_meta(soup: BeautifulSoup) -> datetime | None:
    for key in _META_KEYS:
        for attr in ("name", "property", "itemprop"):
            tag = soup.find("meta", attrs={attr: key})
            if tag is None:
                continue
            parsed = _parse(tag.get("content"))
            if parsed is not None:
                return parsed
    return None


def _walk_for_date_modified(node) -> "str | None":
    """First ``dateModified`` anywhere in a JSON-LD document.

    schema.org blocks are commonly wrapped — in an ``@graph`` array, or nested
    under a ``mainEntity`` — so the value cannot be read off the top level.
    """
    if isinstance(node, dict):
        value = node.get("dateModified")
        if isinstance(value, str) and value.strip():
            return value
        for child in node.values():
            found = _walk_for_date_modified(child)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _walk_for_date_modified(child)
            if found is not None:
                return found
    return None


def _from_json_ld(soup: BeautifulSoup) -> datetime | None:
    """schema.org ``dateModified`` from a JSON-LD block.

    The Veeam Help Center (``prerendered_toc``, 34 sources) publishes its
    revision date only here — no meta tag, no <time> element — so without this
    the largest dated vendor in the corpus reads as undated.
    """
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            document = json.loads(raw)
        except (ValueError, TypeError):
            continue  # a malformed block must not abort the others
        parsed = _parse(_walk_for_date_modified(document))
        if parsed is not None:
            return parsed
    return None


def _from_time_element(soup: BeautifulSoup) -> datetime | None:
    tag = soup.find("time", attrs={"datetime": True})
    return _parse(tag.get("datetime")) if tag else None


def extract_last_updated(
    scoped_html: str | None, page_html: str | None = None
) -> "tuple[datetime | None, str | None]":
    """Return ``(last_updated_at, last_updated_source)`` for one page.

    *scoped_html* is the article body; *page_html* is the whole document when the
    caller still has it. Head metadata is only visible in the latter — the
    raw-HTTP path scopes to the body before this runs, which is why Microsoft
    Learn's ``ms.date`` went unseen while its pages carried a real date all
    along. Falls back to *scoped_html* when no separate document is supplied,
    since the render paths pass the full page as the body.

    Returns ``(None, None)`` when the page declares nothing.
    """
    document = page_html or scoped_html
    if document:
        soup = BeautifulSoup(document, "html.parser")
        found = _from_meta(soup)
        if found is not None:
            return found, SOURCE_VENDOR_META
        # Same tier: JSON-LD is the vendor asserting a date in a machine-readable
        # field, just a different serialisation from a <meta> tag.
        found = _from_json_ld(soup)
        if found is not None:
            return found, SOURCE_VENDOR_META

    if scoped_html:
        found = _from_time_element(BeautifulSoup(scoped_html, "html.parser"))
        if found is not None:
            return found, SOURCE_PAGE_MARKUP

    return None, None
