"""Version-token templating for sources whose URL embeds the product version.

A source's ``url_template`` holds a literal ``{version}`` placeholder; the live
``base_url`` is the template resolved against the product's current version. A
``topic_key`` is the version-independent identity of an article — its URL with
the version token swapped back to ``{version}`` — so the same topic across
versions shares one key and its history continues across a version bump.
"""

VERSION_PLACEHOLDER = "{version}"


def resolve_template(template: str, version: str) -> str:
    """Substitute the product version into a ``{version}`` URL template."""
    return template.replace(VERSION_PLACEHOLDER, version)


def derive_topic_key(url: str, url_template: str | None, version: str | None) -> str:
    """Return the version-independent key for *url*.

    For a templated source, replace the version token — anchored at the
    template's placeholder offset in the shared URL prefix — with ``{version}``.
    Non-templated sources (or a missing version) return *url* unchanged.
    """
    if not url_template or not version or VERSION_PLACEHOLDER not in url_template:
        return url
    prefix = url_template.split(VERSION_PLACEHOLDER, 1)[0]
    if url.startswith(prefix) and url[len(prefix):len(prefix) + len(version)] == version:
        return prefix + VERSION_PLACEHOLDER + url[len(prefix) + len(version):]
    # Version not at the expected offset — fall back to a single replace so a
    # mildly-divergent URL still keys consistently.
    return url.replace(version, VERSION_PLACEHOLDER, 1)


def detect_version_token(base_url: str, version: str) -> str | None:
    """Return a ``url_template`` (the first occurrence of *version* in *base_url*
    replaced by ``{version}``), or None when the version string isn't present."""
    if not version or version not in base_url:
        return None
    return base_url.replace(version, VERSION_PLACEHOLDER, 1)
