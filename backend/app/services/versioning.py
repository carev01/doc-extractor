"""Version-token templating for sources whose URL embeds the product version.

A source's ``url_template`` holds a literal ``{version}`` placeholder; the live
``base_url`` is the template resolved against the product's current version. A
``topic_key`` is the version-independent identity of an article — its URL with
the version token swapped back to ``{version}`` — so the same topic across
versions shares one key and its history continues across a version bump.
"""

import re

VERSION_PLACEHOLDER = "{version}"


def resolve_template(template: str, version: str) -> str:
    """Substitute the product version into a ``{version}`` URL template."""
    return template.replace(VERSION_PLACEHOLDER, version)


def derive_topic_key(url: str | None, url_template: str | None, version: str | None) -> str | None:
    """Return the version-independent key for *url*.

    When the version is known, replace it with ``{version}`` — anchored at the
    template's placeholder offset when a ``url_template`` is available, else by a
    single substring replace. The result is stable regardless of whether
    ``url_template`` is set, so a missing/misconfigured template can never
    silently change an article's key and duplicate the whole source on
    re-extraction (the CommCell incident: a run with a NULL ``url_template`` keyed
    every page by its literal-version URL and re-created ~17.5k articles instead
    of matching the stored ``{version}`` keys). Only a missing version — a
    non-versioned source — leaves *url* untemplated.

    *url* may be ``None`` for a url-less structural TOC node (a Flare "book"/
    section header that carries no page, common in HelpSystem.xml/Toc chunks);
    such entries have no topic identity, so it is returned unchanged rather than
    templated.
    """
    if not url or not version:
        return url
    # Template-anchored replacement (preferred): swap exactly the version segment
    # at the template's placeholder offset.
    if url_template and VERSION_PLACEHOLDER in url_template:
        prefix = url_template.split(VERSION_PLACEHOLDER, 1)[0]
        if url.startswith(prefix) and url[len(prefix):len(prefix) + len(version)] == version:
            return prefix + VERSION_PLACEHOLDER + url[len(prefix) + len(version):]
    # Fallback: templatize by the version substring even without a (matching)
    # template, so the key stays consistent when url_template is absent or the
    # version sits at an unexpected offset. Only when the version actually occurs.
    if version in url:
        return url.replace(version, VERSION_PLACEHOLDER, 1)
    return url


def detect_version_token(base_url: str, version: str) -> str | None:
    """Return a ``url_template`` (the first occurrence of *version* in *base_url*
    replaced by ``{version}``), or None when the version string isn't present."""
    if not version or version not in base_url:
        return None
    return base_url.replace(version, VERSION_PLACEHOLDER, 1)


def _slug(text: str) -> str:
    """Lowercase, keep alphanumerics, collapse everything else to single hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s


def derive_pdf_topic_key(path: list[str]) -> str:
    """Stable topic key for a PDF article from its outline path (ancestor titles +
    own title). Slugged per segment and joined with "/" so re-converting the same
    PDF yields the same key — which keeps incremental diffs stable. Empty path
    (single-segment whole-document fallback) maps to "document"."""
    parts = [_slug(p) for p in path if _slug(p)]
    return "/".join(parts) if parts else "document"
